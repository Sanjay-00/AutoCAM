"""
parser.py  -  AutoCAM CIBIL orchestrator

Detects provider → routes to crif_parser or tu_parser → validates →
LLM fallback (CRIF only) on mismatch.

Public API (unchanged):
    parse(pdf_source, api_key=None) → dict
    debug_blocks(pdf_path)
"""

import re
import json
import time
import fitz  # PyMuPDF

from crif_parser import (
    parse_crif, extract_reported_totals, split_account_blocks,
    credit_profile_summary as crif_credit_profile_summary,
    derog_summary as crif_derog_summary,
)
from crif_parser import _is_closed, _extract_balance, _extract_entity
from crif_commercial_parser import parse_crif_commercial, credit_profile_summary, derog_summary
from tu_parser   import parse_transunion
import ocr_extractor
import html_extractor

# ─────────────────────────────────────────────────────────────────
# EXTRACTION METHOD LABELS  (imported by app.py)
# ─────────────────────────────────────────────────────────────────
METHOD_RULE_BASED     = "Rule-based extraction"
METHOD_LLM_CORRECTION = "LLM correction used"
METHOD_LLM_FULL       = "Full LLM extraction used"
METHOD_OCR            = "OCR (Tesseract) extraction"
METHOD_VISION         = "Gemini Vision fallback used"


# ─────────────────────────────────────────────────────────────────
# PDF TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────────

def _open_doc(pdf_source) -> "fitz.Document":
    if isinstance(pdf_source, str):
        return fitz.open(pdf_source)
    pdf_source.seek(0)
    return fitz.open(stream=pdf_source.read(), filetype="pdf")


def _is_html_source(source) -> bool:
    name = getattr(source, "name", source if isinstance(source, str) else "")
    if isinstance(name, str) and name.lower().endswith((".html", ".htm")):
        return True
    return False


def _read_html(source) -> str:
    if isinstance(source, str):
        with open(source, "rb") as f:
            raw = f.read()
    else:
        source.seek(0)
        raw = source.read()
    return _normalize_text(html_extractor.html_to_text(raw))


def _normalize_text(text: str) -> str:
    return text.replace("\xa0", " ").replace("\u2013", "-").replace("\u2014", "-")


def _extract(doc, on_progress=None) -> tuple:
    """
    Return (text, is_scanned, page_texts). Digital PDFs return embedded text;
    scanned PDFs are OCR'd (Tesseract) so the same text parsers can run, with
    per-page OCR kept for Vision page selection.
    """
    text = _normalize_text("\n".join(page.get_text() for page in doc))
    if len(text.strip()) >= 100:
        return text, False, None

    combined, page_texts = ocr_extractor.ocr_document(doc, on_progress=on_progress)
    if len(combined.strip()) < 100:
        raise ValueError(
            "This PDF appears to be scanned and OCR produced no readable text. "
            "Please upload a clearer or digital CIBIL report."
        )
    return combined, True, page_texts


def extract_text(pdf_source) -> str:
    """Public helper  -  returns report text (OCR'd if the PDF is scanned)."""
    if _is_html_source(pdf_source):
        return _read_html(pdf_source)
    doc = _open_doc(pdf_source)
    try:
        return _extract(doc)[0]
    finally:
        doc.close()


# ─────────────────────────────────────────────────────────────────
# PROVIDER DETECTION
# ─────────────────────────────────────────────────────────────────

def _detect_provider(text: str) -> str:
    # Collapse whitespace so OCR's variable spacing (and row-join spacing) doesn't
    # break the literal phrase matches below.
    sample = re.sub(r'\s+', ' ', text[:6000]).lower()
    if ("transunion" in sample or "tu cibil" in sample
            or "cibil msme rank" in sample or "cmr-" in sample
            or "commercial credit information report" in sample):
        return "transunion"
    if (re.search(r'commercial\s*ace\W*report', sample) or "perform commercial" in sample
            or "borrower summary" in sample):
        return "crif_commercial"
    return "crif"


# ─────────────────────────────────────────────────────────────────
# CRIF VALIDATION
# ─────────────────────────────────────────────────────────────────

def validate_extraction(accounts: list, reported: dict, amount_floor: int = 1000,
                         overdue_floor: int = 50_000) -> dict:
    """CRIF validation: active count + active balance (+ sanction/overdue on
    CRIF Commercial, where the Borrower Summary prints them) vs the report's
    own summary.

    amount_floor is the minimum absolute tolerance under the 5% relative
    check (balance/sanction only), in rupees. CRIF Retail's Account Summary
    prints exact rupee digits, so the default Rs.1000 (rounding/OCR noise) is
    right there. CRIF Commercial's Borrower Summary instead prints
    2-decimal Crores - Rs.1,00,000 (1 lakh) per unit - so the caller passes
    Rs.50,000 (half a lakh) for that provider; using the tighter default
    there would flag the bureau's own summary-table rounding as an
    extraction error.

    overdue_floor is a flat (not %-of-expected) absolute tolerance for
    Overdue specifically - deliberately no percentage component at all,
    since a 5%-of-expected check breaks down exactly where it matters most:
    Overdue is very often reported as Rs.0 (nothing overdue), and 5% of
    zero is zero, which would flag ANY genuinely-small real overdue amount
    (e.g. our Rs.30,000 vs the bureau's Rs.0) as a mismatch even though it's
    well within the summary table's own Rs.1-lakh rounding precision."""
    issues     = []
    active     = [a for a in accounts if a.get("status") == "Active"]
    count      = len(active)
    balance    = sum(a.get("current_balance") or 0 for a in active)
    sanction   = sum(a.get("sanction_amount") or 0 for a in active)
    overdue    = sum(a.get("overdue") or 0 for a in active)
    delinquent = sum(1 for a in active if a.get("delinquent"))

    exp_count    = reported.get("account_count")
    exp_bal      = reported.get("total_balance")
    exp_sanction = reported.get("total_sanction")
    exp_overdue  = reported.get("total_overdue")

    # Per-field pass/fail, computed with the exact same thresholds used below
    # to raise issues - callers (app.py's validation badge) must read these
    # rather than re-deriving their own tolerance, so the UI's tick/cross can
    # never disagree with what actually determined `valid`.
    balance_ok  = (not exp_bal) or abs(balance - exp_bal) <= max(exp_bal * 0.05, amount_floor)
    sanction_ok = (not exp_sanction) or abs(sanction - exp_sanction) <= max(exp_sanction * 0.05, amount_floor)
    overdue_ok  = (exp_overdue is None) or abs(overdue - exp_overdue) <= overdue_floor

    # Zero accounts extracted is only a genuine pass when the report's own
    # summary totals confirm it (e.g. a real thin-file/no-trade-history
    # report, where account_count comes back 0 rather than None). If we
    # extracted nothing AND couldn't find the summary totals either, that's
    # not verified - it just means we have no ground truth to check against,
    # which is exactly what a silent block-splitting failure looks like too.
    # Flag it instead of reporting a clean "valid" with nothing behind it.
    if not accounts and exp_count is None and exp_bal is None:
        issues.append(
            "No accounts extracted and the report's own summary totals "
            "could not be found either - this could be a genuinely empty "
            "report, or a parsing failure. Please check the source manually."
        )
    else:
        # CRIF Commercial's "Live Accts" figure deliberately excludes
        # delinquent-but-open accounts, while our extraction correctly
        # counts a delinquent (still open, balance > 0) facility as Active -
        # see crif_commercial_parser._parse_summary_row_full. When the gap
        # is fully explained by that (extracted active minus delinquent
        # equals the report's own count), it's not an extraction error, so
        # don't raise it as one - a mismatch that isn't explained this way
        # still gets flagged, same as before.
        if exp_count is not None and count != exp_count:
            if not (delinquent and count - delinquent == exp_count):
                issues.append(
                    f"Active account count mismatch: extracted {count}, "
                    f"report says {exp_count}"
                )
        if exp_bal and exp_bal > 0:
            if abs(balance - exp_bal) > max(exp_bal * 0.05, amount_floor):
                issues.append(
                    f"Balance mismatch: extracted Rs.{balance:,}, "
                    f"report says Rs.{exp_bal:,}"
                )
        if exp_sanction and exp_sanction > 0:
            if abs(sanction - exp_sanction) > max(exp_sanction * 0.05, amount_floor):
                issues.append(
                    f"Sanctioned amount mismatch: extracted Rs.{sanction:,}, "
                    f"report says Rs.{exp_sanction:,}"
                )
        if exp_overdue is not None:
            if abs(overdue - exp_overdue) > overdue_floor:
                issues.append(
                    f"Overdue amount mismatch: extracted Rs.{overdue:,}, "
                    f"report says Rs.{exp_overdue:,}"
                )

    return {
        "valid":               len(issues) == 0,
        "issues":              issues,
        "extracted_count":     count,
        "extracted_balance":   balance,
        "extracted_sanction":  sanction,
        "extracted_overdue":   overdue,
        "expected_count":      exp_count,
        "expected_balance":    exp_bal,
        "expected_sanction":   exp_sanction,
        "expected_overdue":    exp_overdue,
        "delinquent_active_count": delinquent,
        "balance_ok":          balance_ok,
        "sanction_ok":         sanction_ok,
        "overdue_ok":          overdue_ok,
    }


# ─────────────────────────────────────────────────────────────────
# LLM FALLBACK  (CRIF only)
# ─────────────────────────────────────────────────────────────────

_LLM_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
]


def _content_to_text(content) -> str:
    """
    Normalise a LangChain response .content to plain text. Newer Gemini models
    return a list of parts (e.g. {'type': 'text', 'text': ...}) instead of a
    string; concatenate the text parts.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                parts.append(p.get("text", ""))
            else:
                parts.append(str(p))
        return "".join(parts)
    return str(content)


def _is_transient(err: Exception) -> bool:
    """Rate-limit / overload / timeout errors - worth a short backoff+retry
    rather than either failing the call outright or burning a model swap."""
    s = str(err)
    return any(tok in s for tok in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE",
                                     "DeadlineExceeded", "Timeout"))


def _llm_invoke(api_key: str, prompt) -> str:
    """
    Invoke the Gemini model cascade. `prompt` may be a plain string or a
    multimodal content list (text + image_url parts) for Vision extraction.

    Transient errors (rate-limit/overload) get a short backoff-retry on the
    SAME model before moving on - these calls run in parallel batches
    (see _enrich_dpd_vision), so a burst of 429s is expected and recoverable
    within a couple seconds rather than a real failure.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage
    except ImportError:
        raise RuntimeError("langchain_google_genai not installed")

    for model in _LLM_MODELS:
        llm = ChatGoogleGenerativeAI(
            model=model, google_api_key=api_key,
            temperature=0.1, max_tokens=8192,
        )
        last_err = None
        for delay in (0, 1.5, 3):
            if delay:
                time.sleep(delay)
            try:
                return _content_to_text(llm.invoke([HumanMessage(content=prompt)]).content)
            except Exception as e:
                last_err = e
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    break  # model doesn't exist - no point retrying, try next one
                if not _is_transient(e):
                    raise
        if last_err and ("404" in str(last_err) or "NOT_FOUND" in str(last_err)):
            continue
    raise RuntimeError(f"No Gemini model responded. Tried: {_LLM_MODELS}")


def _strip_md(text: str) -> str:
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    return re.sub(r'\s*```$', '', text).strip()


def _normalize(accounts: list) -> list:
    for acc in accounts:
        for f in ("sr_no", "sanction_amount", "current_balance", "emi", "overdue"):
            try:
                acc[f] = int(float(str(acc.get(f, 0)).replace(",", "")))
            except (ValueError, TypeError):
                acc[f] = 0
        # max_dpd: unlike the fields above, None is a meaningful value here -
        # it means Gemini couldn't read delinquency, not that it's 0/clean.
        # Preserve it (rendered as "Check CIBIL") instead of defaulting to 0.
        dpd = acc.get("max_dpd")
        if dpd is None:
            acc["max_dpd"] = None
        else:
            try:
                acc["max_dpd"] = int(float(str(dpd).replace(",", "")))
            except (ValueError, TypeError):
                acc["max_dpd"] = None
        if not acc.get("date_of_sanction"):
            acc["date_of_sanction"] = "NA"
        if acc.get("status", "").lower() not in ("active", "closed"):
            acc["status"] = "Active"
    return accounts


def _llm_fix_blocks(blocks: list, current: list, api_key: str) -> tuple:
    import json
    blocks_text = "\n\n__ACCOUNT__\n\n".join(
        f"ACCOUNT {num}:\n{blk[:1800]}" for num, blk in blocks
    )
    prompt = (
        "You are a financial data extraction expert correcting CIBIL account data.\n"
        "The rule-based extraction below has validation errors. Fix only wrong fields.\n\n"
        f"CURRENT EXTRACTION:\n{json.dumps(current, indent=2)}\n\n"
        f"RAW ACCOUNT BLOCKS:\n{blocks_text[:14000]}\n\n"
        "Return ONLY a valid JSON array. Keys: sr_no, date_of_sanction, sanction_amount, "
        "current_balance, emi, overdue, entity, type_of_loan, max_dpd, status. "
        "Do NOT add or remove accounts."
    )
    try:
        raw   = _llm_invoke(api_key, prompt)
        fixed = __import__("json").loads(_strip_md(raw))
        if isinstance(fixed, list) and fixed:
            return _normalize(fixed), True
    except Exception:
        pass
    return current, False


def _llm_full(text: str, api_key: str, expected_count) -> tuple:
    hint   = f"There should be {expected_count} accounts." if expected_count else ""
    prompt = (
        f"Extract ALL loan accounts from this CIBIL credit report. {hint}\n\n"
        "Return ONLY a valid JSON array. Keys: sr_no, date_of_sanction, sanction_amount, "
        "current_balance, emi, overdue, entity, type_of_loan, max_dpd, status.\n\n"
        f"CIBIL TEXT:\n{text[:28000]}"
    )
    try:
        raw      = _llm_invoke(api_key, prompt)
        accounts = __import__("json").loads(_strip_md(raw))
        if isinstance(accounts, list) and accounts:
            return _normalize(accounts), True
    except Exception:
        pass
    return [], False


# ─────────────────────────────────────────────────────────────────
# MAIN PARSE FUNCTION
# ─────────────────────────────────────────────────────────────────

def _renumber(accounts: list) -> None:
    accounts.sort(key=lambda x: x.get("sr_no", 0))
    for i, acc in enumerate(accounts, 1):
        acc["sr_no"] = i


def _vision_postprocess(raw: str) -> list:
    """Parse a Gemini Vision JSON reply into normalised account dicts."""
    return _normalize(json.loads(_strip_md(raw)))


def _val_quality(v: dict) -> tuple:
    """
    Rank a validation result: valid beats invalid; among equals, smaller combined
    relative error (count + balance) wins. Used to keep the better of the OCR
    rule-based result vs the Vision fallback.
    """
    err = 0.0
    ec, xc = v.get("extracted_count"), v.get("expected_count")
    if xc:
        err += abs((ec or 0) - xc) / xc
    eb, xb = v.get("extracted_balance"), v.get("expected_balance")
    if xb:
        err += abs((eb or 0) - xb) / xb
    return (1 if v.get("valid") else 0, -err)


def _find_account_page(acc: dict, page_texts: list) -> int | None:
    """Return the page index whose OCR text contains this account's sanction date."""
    date = acc.get("date_of_sanction", "")
    if not date or date == "NA":
        return None
    for pg_idx, pg_text in enumerate(page_texts):
        if date in pg_text:
            return pg_idx
    return None


def _enrich_dpd_vision(accounts: list, doc, page_texts: list, api_key: str,
                       on_progress=None) -> dict:
    """
    For scanned CRIF Commercial PDFs, OCR cannot read text inside coloured
    (orange/red) payment history cells, and _extract_max_dpd returns None for
    accounts where it found no readable payment-history pattern at all (shown
    as "Check CIBIL" rather than a possibly-wrong 0). This function sends each
    affected page to Gemini Vision and resolves max_dpd on those None accounts.
    Confident 0-DPD reads from OCR are left untouched - only genuinely unread
    accounts are sent. Mutates accounts in-place; called only when
    method != METHOD_VISION.

    Only pages that actually hold an unread (None) account are rendered and
    sent - not the whole document - to keep this fast and cheap.

    Pages are rendered on the main thread (PyMuPDF is not thread-safe) then
    all Gemini API calls run in parallel, turning ~N×10s into ~10s total.
    on_progress(done, total) fires as each Vision call completes.

    Returns a summary dict the caller can show in the UI:
        pages_sent      - sorted 1-indexed page numbers sent to Gemini
        accounts_checked- sr_no of every account examined (was None/unread from OCR)
        accounts_patched- sr_no of accounts whose max_dpd Gemini actually resolved
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Group accounts whose DPD OCR genuinely couldn't read (None) by PDF page.
    # Confident 0-DPD reads are trusted as-is and NOT re-checked here.
    page_map: dict[int, list] = {}
    for acc in accounts:
        if acc.get("max_dpd") is None:
            pg = _find_account_page(acc, page_texts)
            if pg is not None:
                page_map.setdefault(pg, []).append(acc)

    summary = {"pages_sent": [], "accounts_checked": [], "accounts_patched": []}
    if not page_map:
        return summary

    summary["pages_sent"]       = sorted(pg + 1 for pg in page_map)
    summary["accounts_checked"] = sorted(acc["sr_no"] for accs in page_map.values() for acc in accs)

    # Render all page images on the main thread first. A single malformed page
    # (corrupt embedded image, bad content stream) must not abort the whole
    # extraction - skip it and leave its accounts unresolved (Check CIBIL)
    # rather than losing every already-validated account to one bad render.
    page_uris = {}
    for pg_idx in page_map:
        try:
            page_uris[pg_idx] = ocr_extractor._img_data_uri(doc[pg_idx])
        except Exception:
            continue

    total = len(page_uris)
    if not total:
        return summary
    done_count = 0

    def _call(pg_idx):
        return pg_idx, ocr_extractor.vision_extract_dpd_from_uri(
            page_uris[pg_idx], page_map[pg_idx], api_key, _llm_invoke
        )

    # I/O-bound (network latency dominates) - the retry-with-backoff in
    # _llm_invoke absorbs the 429 bursts this concurrency causes, so raising
    # it speeds up wall-clock time without trading away accuracy.
    with ThreadPoolExecutor(max_workers=min(total, 12)) as pool:
        futures = {pool.submit(_call, pg_idx): pg_idx for pg_idx in page_uris}
        for fut in as_completed(futures):
            done_count += 1
            if on_progress:
                on_progress(done_count, total)
            try:
                pg_idx, dpd_map = fut.result()
            except Exception:
                continue
            for acc in page_map[pg_idx]:
                key = f"{acc['date_of_sanction']}|{acc.get('sanction_amount') or 0}"
                # Accept whatever Gemini reports, including 0 - these accounts
                # started as None (unread), so even a confirmed 0 resolves the
                # uncertainty and is worth recording. Left as None (Check CIBIL)
                # only if Gemini has no answer for this key at all.
                if key in dpd_map:
                    acc["max_dpd"] = dpd_map[key]
                    summary["accounts_patched"].append(acc["sr_no"])

    summary["accounts_patched"].sort()
    return summary


def _parse_crif_commercial(text, doc, scanned, page_texts, api_key,
                           on_dpd_progress=None, enrich_dpd: bool = True) -> dict:
    """
    CRIF Commercial ACE path. Rule-based on the (possibly OCR'd) text is always
    the default for scanned reports - Gemini (both the full-account Vision
    fallback and the DPD colour-cell enrichment) only runs when the caller
    opts in via `enrich_dpd` (the "Enrich DPD via Vision (Gemini)" checkbox in
    the UI). If validation fails on a scanned report and the user hasn't opted
    in, we surface a recommendation instead of silently calling Gemini.

    When opted in: if the parse fails the report's summary validation, fall
    back to Gemini Vision on the targeted account pages - but only adopt the
    Vision result if it validates at least as well as the OCR result. Then a
    second Vision pass (_enrich_dpd_vision) resolves max_dpd on accounts where
    OCR found no readable payment-history pattern at all (None/"Check CIBIL"),
    by reading the page image directly. Confident 0-DPD OCR reads are left
    untouched. on_dpd_progress(done, total) fires after each page Vision call
    completes.
    """
    name, score, blocks, accounts, reported, analysis = parse_crif_commercial(text, scanned)
    _renumber(accounts)

    # Borrower Summary carries Sanctioned/Overdue totals alongside the
    # Live-Accts/Outstanding pair extract_reported_totals() already puts in
    # `reported` - fold them in here so validate_extraction() can check them
    # too, the same way it already checks balance.
    bs = analysis.get("borrower_summary") or {}
    yi, oi = bs.get("your_institution") or {}, bs.get("other_institution") or {}
    if yi.get("sanctioned_amt") is not None or oi.get("sanctioned_amt") is not None:
        reported["total_sanction"] = (yi.get("sanctioned_amt") or 0) + (oi.get("sanctioned_amt") or 0)
    if yi.get("overdue_amt") is not None or oi.get("overdue_amt") is not None:
        reported["total_overdue"] = (yi.get("overdue_amt") or 0) + (oi.get("overdue_amt") or 0)

    method     = METHOD_OCR if scanned else METHOD_RULE_BASED
    # CRIF Commercial's amounts come from the Borrower Summary's 2-decimal-Crore
    # figures (1 lakh precision) - see validate_extraction()'s amount_floor note.
    validation = validate_extraction(accounts, reported, amount_floor=50_000)

    # Recommend the Gemini fallback rather than using it automatically - only
    # runs once the user has ticked the checkbox (enrich_dpd).
    vision_fallback_recommended = not validation["valid"] and scanned and bool(api_key)
    vision_fallback_used        = False

    if vision_fallback_recommended and enrich_dpd:
        vision_fallback_used = True
        pages = ocr_extractor.select_pages(page_texts) if page_texts else []
        vis = ocr_extractor.vision_extract_accounts(
            doc, pages, api_key,
            invoke_fn=_llm_invoke, postprocess_fn=_vision_postprocess,
        )
        if vis:
            _renumber(vis)
            v_vis = validate_extraction(vis, reported, amount_floor=50_000)
            if _val_quality(v_vis) > _val_quality(validation):
                accounts, validation, method = vis, v_vis, METHOD_VISION

    # DPD enrichment: runs even when validation passed; skipped if Vision already
    # extracted the full account set (which includes DPD from the image).
    # dpd_vision_recommended flags reports that actually have unread (None) DPD
    # accounts worth resolving via Gemini - lets the UI nudge the user only when
    # there's really something to check, not on every scanned report.
    has_unread_dpd = any(a.get("max_dpd") is None for a in accounts)
    dpd_vision_recommended = scanned and bool(api_key) and method != METHOD_VISION and has_unread_dpd
    dpd_vision_used        = False
    dpd_vision_summary     = {"pages_sent": [], "accounts_checked": [], "accounts_patched": []}
    if enrich_dpd and dpd_vision_recommended and page_texts:
        dpd_vision_used    = True
        dpd_vision_summary = _enrich_dpd_vision(accounts, doc, page_texts, api_key,
                                                on_progress=on_dpd_progress)

    # Vision fallback / DPD enrichment above can replace or patch `accounts`
    # after parse_crif_commercial() built `analysis` - the two account-derived
    # sections need recomputing against the FINAL list so they match what's
    # actually shown in the accounts table. borrower_summary is parsed from
    # the report text directly, unaffected by any of that, so it's left as-is.
    analysis["credit_profile_summary"] = credit_profile_summary(accounts)
    analysis["derog_summary"]          = derog_summary(accounts)

    return {
        "name":                   name,
        "score":                  score,
        "accounts":               accounts,
        "extraction_method":      method,
        "validation":             validation,
        "provider":               "crif_commercial",
        "analysis":               analysis,
        "tesseract_version":      ocr_extractor.tesseract_version() if scanned else None,
        "vision_fallback_recommended": vision_fallback_recommended,
        "vision_fallback_used":        vision_fallback_used,
        "dpd_vision_recommended": dpd_vision_recommended,
        "dpd_vision_used":        dpd_vision_used,
        "dpd_vision_pages":       dpd_vision_summary["pages_sent"],
        "dpd_vision_checked":     dpd_vision_summary["accounts_checked"],
        "dpd_vision_patched":     dpd_vision_summary["accounts_patched"],
    }


def parse(pdf_source, api_key: str = None, on_progress=None,
          on_dpd_progress=None, enrich_dpd: bool = False) -> dict:
    """
    Parse a CIBIL PDF (CRIF retail, CRIF Commercial, or TransUnion) and return
    structured data. Scanned PDFs are OCR'd first.

    on_progress(current_page, total_pages) is called during OCR if provided.
    on_dpd_progress(done, total) is called during Vision DPD enrichment (CRIF
    Commercial scanned only).
    enrich_dpd: CRIF Commercial only, opt-in (default False). Rule-based OCR
    is always tried first; Gemini is never called unless this is True - the
    UI surfaces `vision_fallback_recommended` / `dpd_vision_recommended` in
    the result so the user can decide whether to re-run with it enabled.
    HTML sources (.html/.htm) are supported too  -  they carry embedded text
    (like a digital PDF) with no OCR/Vision path, since there's no PDF page to
    render.

    Returns dict:
        name, score, accounts, extraction_method, validation, provider
    """
    if _is_html_source(pdf_source):
        text, scanned, page_texts, doc = _read_html(pdf_source), False, None, None
        return _parse_text(text, scanned, page_texts, doc, api_key,
                           on_dpd_progress=on_dpd_progress, enrich_dpd=enrich_dpd)

    doc = _open_doc(pdf_source)
    try:
        text, scanned, page_texts = _extract(doc, on_progress=on_progress)
        return _parse_text(text, scanned, page_texts, doc, api_key,
                           on_dpd_progress=on_dpd_progress, enrich_dpd=enrich_dpd)
    finally:
        doc.close()


def _parse_text(text, scanned, page_texts, doc, api_key,
                on_dpd_progress=None, enrich_dpd: bool = True) -> dict:
    provider = _detect_provider(text)

    # ── TransUnion path ───────────────────────────────────────
    if provider == "transunion":
        name, score, accounts, reported, validation = parse_transunion(text)
        _renumber(accounts)
        return {
            "name":              name,
            "score":             score,
            "accounts":          accounts,
            "extraction_method": METHOD_OCR if scanned else METHOD_RULE_BASED,
            "validation":        validation,
            "provider":          "transunion",
            "tesseract_version": ocr_extractor.tesseract_version() if scanned else None,
        }

    # ── CRIF Commercial ACE path ──────────────────────────────
    if provider == "crif_commercial":
        return _parse_crif_commercial(text, doc, scanned, page_texts, api_key,
                                      on_dpd_progress=on_dpd_progress,
                                      enrich_dpd=enrich_dpd)

    # ── CRIF retail path ──────────────────────────────────────
    name, score, blocks, accounts, reported = parse_crif(text)
    _renumber(accounts)

    extraction_method = METHOD_OCR if scanned else METHOD_RULE_BASED
    validation        = validate_extraction(accounts, reported)

    # Stage 2: LLM block-fix
    if not validation["valid"] and api_key:
        fixed, ok = _llm_fix_blocks(blocks, accounts, api_key)
        if ok:
            _renumber(fixed)
            v2 = validate_extraction(fixed, reported)
            if v2["valid"]:
                accounts          = fixed
                extraction_method = METHOD_LLM_CORRECTION
                validation        = v2
            else:
                # Stage 3: Full-PDF LLM
                full, ok2 = _llm_full(text, api_key, reported.get("account_count"))
                if ok2 and full:
                    _renumber(full)
                    accounts          = full
                    extraction_method = METHOD_LLM_FULL
                    validation        = validate_extraction(accounts, reported)

    return {
        "name":              name,
        "score":             score,
        "accounts":          accounts,
        "extraction_method": extraction_method,
        "validation":        validation,
        "provider":          "crif",
        "tesseract_version": ocr_extractor.tesseract_version() if scanned else None,
        "analysis": {
            "credit_profile_summary": crif_credit_profile_summary(accounts),
            "derog_summary":          crif_derog_summary(accounts),
        },
    }


# ─────────────────────────────────────────────────────────────────
# DEBUG UTILITY  (python parser.py <pdf_path>)
# ─────────────────────────────────────────────────────────────────

def debug_blocks(pdf_path: str) -> None:
    text   = extract_text(pdf_path)
    blocks = split_account_blocks(text)
    rep    = extract_reported_totals(text)

    print(f"\n{'='*60}")
    print(f"  Blocks found    : {len(blocks)}")
    print(f"  Expected active : {rep.get('account_count', 'not found')}")
    print(f"  Expected balance: {rep.get('total_balance', 'not found')}")
    print(f"{'='*60}\n")

    for acct_num, block in blocks:
        status = "CLOSED" if _is_closed(block) else "active"
        bal    = _extract_balance(block)
        entity = _extract_entity(block)
        print(f"  [{acct_num:>3}] {status:<8}  bal={bal:>12,}  entity={entity}")
        print(f"         raw: {block[:120].replace(chr(10), '↵ ')}")
        print()

    all_raw = re.findall(r'Account\s+Information[\s\S]{0,60}', text)
    print(f"All 'Account Information' occurrences ({len(all_raw)})")
    for hit in all_raw:
        print(f"  {hit.replace(chr(10), '↵ ')[:80]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parser.py <path_to_pdf>")
    else:
        debug_blocks(sys.argv[1])
