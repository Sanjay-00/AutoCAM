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
    extract_borrower_identity,
)
from crif_parser import _is_closed, _extract_balance, _extract_entity
from crif_parser import _extract_max_dpd as _crif_extract_max_dpd
from crif_parser import _extract_dpd_window as _crif_extract_dpd_window
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

    page_texts is populated for digital PDFs too (not just scanned) - the
    Vision page-locator (_find_account_page) needs a real per-page list to
    target a digital-report Vision fallback (e.g. recovering DPD on a block
    whose text-extraction reading order got scrambled across account
    boundaries - see _dpd_span_contaminated), not just an OCR one.
    """
    page_texts_raw = [page.get_text() for page in doc]
    text = _normalize_text("\n".join(page_texts_raw))
    if len(text.strip()) >= ocr_extractor._SCAN_TEXT_THRESHOLD:
        return text, False, [_normalize_text(t) for t in page_texts_raw]

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
                         overdue_floor: int = 50_000,
                         overdue_scope_all_accounts: bool = False) -> dict:
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
    well within the summary table's own Rs.1-lakh rounding precision.

    overdue_scope_all_accounts controls which accounts the Overdue comparison
    sums over - unlike Total Current Balance (which both providers' own
    report text explicitly scopes to ACTIVE accounts only), the two
    providers' Overdue totals are NOT scoped the same way:
      - CRIF Retail's Account Summary "Total Amount Overdue" includes CLOSED
        accounts too (a written-off account can still carry a residual
        overdue balance at closure) - confirmed on real reports where the
        summary's overdue figure only reconciled once closed accounts were
        included (e.g. one real report's entire Rs.1,53,857 overdue lived on
        a single Closed account; an active-only sum read Rs.0 there and
        failed validation against a real, correctly-extracted figure).
      - CRIF Commercial's Borrower Summary Overdue figure is active-only
        (consistent with its Live-Accts-only scoping used throughout this
        codebase) - confirmed on a real report where switching to an
        all-accounts sum would have pushed a currently-passing report
        Rs.3.68L off from the summary (comfortably outside overdue_floor),
        while the active-only sum landed within Rs.23k of it.
    Callers must pass the right scope for their provider; getting it backwards
    turns a real, correct extraction into a false "mismatch" on one provider
    while masking a real one on the other."""
    issues     = []
    active     = [a for a in accounts if a.get("status") == "Active"]
    count      = len(active)
    balance    = sum(a.get("current_balance") or 0 for a in active)
    sanction   = sum(a.get("sanction_amount") or 0 for a in active)
    overdue    = sum(a.get("overdue") or 0
                     for a in (accounts if overdue_scope_all_accounts else active))
    delinquent = sum(1 for a in active if a.get("delinquent"))

    exp_count    = reported.get("account_count")
    exp_bal      = reported.get("total_balance")
    exp_sanction = reported.get("total_sanction")
    exp_overdue  = reported.get("total_overdue")

    # An account whose loan type came back "Unknown" is a proxy for a block
    # whose header row OCR'd too badly for even the fuzzy vocabulary match in
    # _extract_loan_type to recover - confirmed on a real scanned report
    # where two accounts' header rows OCR'd into unrelated garbage words
    # (e.g. "COMMERCIAL VEHICLE cist" / "LOAN 08 pe 3105-2005") even though
    # every other field in the same block, and every neighbouring account's
    # header on the same page, read perfectly cleanly - a narrow, row-level
    # OCR failure rather than a page-wide quality problem. Balance/count can
    # still happen to reconcile against the report's own summary in that
    # case (or there may be no summary to check against at all, as here),
    # so this needs its own signal - it's the only way this class of error
    # ever surfaces, and it's what lets the Stage 2/3 LLM correction cascade
    # (which reads from the same raw block text and can usually reconstruct
    # the type from the surviving fragments) actually get a chance to run.
    unknown_type = [a for a in accounts if (a.get("type_of_loan") or "Unknown") == "Unknown"]
    if unknown_type:
        issues.append(
            f"{len(unknown_type)} account(s) have an unrecognized loan type "
            "- likely OCR corruption in that account's header row. Please "
            "check manually."
        )

    # An account can't be genuinely overdue by a real amount while also
    # having 0 days past due - that's a logical contradiction, not a real
    # report state, so it's a ground-truth-free signal that the Payment
    # History grid's OCR lost the actual DPD (confirmed on a real scanned
    # report where several accounts' DPD grid cells were shaded - and
    # therefore harder for Tesseract to segment cleanly - and came back
    # reading as 0 instead of their real value, e.g. 45 or 83, while the
    # separately-extracted Overdue Amt field stayed correct throughout).
    # None (grid genuinely unread, CRIF Commercial only) is excluded - that's
    # already surfaced elsewhere ("Check CIBIL"), this is specifically about
    # a *wrong* 0, not a missing one.
    dpd_contradiction = [a for a in accounts
                          if (a.get("overdue") or 0) > 1000 and a.get("max_dpd") == 0]
    if dpd_contradiction:
        issues.append(
            f"{len(dpd_contradiction)} account(s) show a real overdue amount "
            "but 0 days past due - likely OCR loss in that account's Payment "
            "History grid. Please check manually."
        )

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
        "unknown_type_count":  len(unknown_type),
        "dpd_contradiction_count": len(dpd_contradiction),
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
        # written_off: Gemini may omit the key entirely rather than return an
        # explicit false - without this, Retail's derog rollup would silently
        # read every LLM-corrected account as "not written off" regardless of
        # what the report actually says (missing key == falsy == same as a
        # confident False read, no way to tell them apart downstream).
        acc["written_off"] = bool(acc.get("written_off"))
    return accounts


def _reattach_ownership(new_accounts: list, old_accounts: list) -> list:
    """
    LLM/Vision correction and full re-extraction paths (_llm_fix_blocks,
    _llm_full, and CRIF Commercial's vision_extract_accounts) request their
    own field list from the model, which doesn't include `ownership` -
    silently dropping it loses a real bureau-reported field (Guarantor/
    Joint-capacity accounts becoming indistinguishable from Individual
    ones after fallback correction). `ownership` is a bureau-formatting-
    derived field (parsed from fixed label text, not free-form prose) that
    an LLM correcting other fields has no reason to touch, so it's cheaper
    and lower-risk to reattach it from the pre-fallback rule-based
    extraction than to teach every prompt to preserve it. Matched by
    sr_no (each fallback is asked not to add/remove accounts, so sr_no
    should still line up); falls back to positional index when sr_no
    drifted or the fallback is a full independent re-extraction.
    """
    if not old_accounts:
        return new_accounts
    by_sr = {a.get("sr_no"): a.get("ownership") for a in old_accounts if a.get("sr_no") is not None}
    for i, acc in enumerate(new_accounts):
        if acc.get("ownership"):
            continue
        own = by_sr.get(acc.get("sr_no"))
        if not own and i < len(old_accounts):
            own = old_accounts[i].get("ownership")
        acc["ownership"] = own or ""
    return new_accounts


def _llm_fix_blocks(blocks: list, current: list, api_key: str) -> tuple:
    blocks_text = "\n\n__ACCOUNT__\n\n".join(
        f"ACCOUNT {num}:\n{blk[:1800]}" for num, blk in blocks
    )
    prompt = (
        "You are a financial data extraction expert correcting CIBIL account data.\n"
        "The rule-based extraction below has validation errors. Fix only wrong fields.\n\n"
        f"CURRENT EXTRACTION:\n{json.dumps(current, indent=2)}\n\n"
        f"RAW ACCOUNT BLOCKS:\n{blocks_text[:14000]}\n\n"
        "Return ONLY a valid JSON array. Keys: sr_no, date_of_sanction, sanction_amount, "
        "current_balance, emi, overdue, entity, type_of_loan, max_dpd, status, "
        "written_off (true only if Remarks says written-off or there's a non-zero "
        "write-off amount - false otherwise). "
        "Do NOT add or remove accounts."
    )
    try:
        raw   = _llm_invoke(api_key, prompt)
        fixed = json.loads(_strip_md(raw))
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
        "current_balance, emi, overdue, entity, type_of_loan, max_dpd, status, "
        "written_off (true only if Remarks says written-off or there's a non-zero "
        "write-off amount - false otherwise).\n\n"
        f"CIBIL TEXT:\n{text[:28000]}"
    )
    try:
        raw      = _llm_invoke(api_key, prompt)
        accounts = json.loads(_strip_md(raw))
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


_ACCOUNT_INFO_HDR_RE = re.compile(r'Account\s+Information', re.IGNORECASE)
_PAYMENT_HISTORY_RE  = re.compile(r'Payment\s+History', re.IGNORECASE)


def _grid_spills_to_next_page(page_text: str, match_pos: int) -> bool:
    """
    True when this account's own Payment History grid is provably NOT on the
    same page as its header/fields - confirmed on a real report: the account
    box can be split by a page break with fields on one page and the ENTIRE
    "Payment History/Asset Classification:" section (label included) pushed
    onto the next. Sending Vision only the located page in that case gives it
    no grid to read at all, which produced two different failure shapes on a
    real report - a safe null (no data, correctly declined) and a wrong-but-
    plausible guessed 0 (a confident wrong number) - depending on the call,
    neither acceptable when the real answer is one page over. The caller
    renders the next page too when this is True.
    """
    next_hdr = _ACCOUNT_INFO_HDR_RE.search(page_text, match_pos + 1)
    boundary = next_hdr.start() if next_hdr else len(page_text)
    return not _PAYMENT_HISTORY_RE.search(page_text, match_pos, boundary)


def _account_anchor_amount(acc: dict):
    """
    An amount field specific enough to locate on a page via text search - NOT
    just any non-None amount. Current Balance is preferred (effectively
    unique per account) but a Closed/written-off account commonly has it at
    exactly 0 (confirmed on a real report), and "0" as a search pattern
    matches almost any digit anywhere on the page - silently anchoring on a
    random, unrelated "0" instead of failing loudly. Falls back to Sanctioned
    Amount (Disbd Amt/High Credit), which is virtually always a real,
    specific loan amount even for a written-off account. Returns None only
    when neither field clears the specificity floor, so callers correctly
    fall back further (date-only, or "can't verify") instead of anchoring on
    a near-universal digit.
    """
    for amt in (acc.get("current_balance"), acc.get("sanction_amount")):
        if amt is not None and abs(amt) >= 100:
            return amt
    return None


def _find_account_page(acc: dict, page_texts: list) -> tuple:
    """
    Return (page_index, grid_spills_to_next_page) for this account's own
    Payment History grid, or (None, False) if no page matches at all.

    Sanctioned Date alone is not a safe anchor - sibling accounts (a guarantor
    obligation split across several loans) commonly share the same date, so a
    plain "date in page text" search can return a DIFFERENT account's page
    (confirmed on a real report: two guarantor CV loans sanctioned the same
    day, one page apart - date-only lookup sent Vision the wrong page for the
    second one, and it correctly reported the account "not found" there).
    An amount anchor (_account_anchor_amount) is checked first because it's
    effectively unique per account (unlike the date); matched allowing
    optional Indian comma grouping between digits ("11,95,399") so the
    position within the page is still known (needed for
    _grid_spills_to_next_page), not just digit-stripped presence. Falls back
    to date-only when no amount clears the specificity floor or isn't found
    on any page.
    """
    date = acc.get("date_of_sanction", "")
    date_ok = bool(date and date != "NA")
    bal = _account_anchor_amount(acc)
    bal_digits = re.sub(r'\D', '', str(bal)) if bal is not None else None
    bal_pattern = re.compile(r',?'.join(re.escape(d) for d in bal_digits)) if bal_digits else None

    bal_only_match, date_only_match = None, None
    for pg_idx, pg_text in enumerate(page_texts):
        bal_m    = bal_pattern.search(pg_text) if bal_pattern else None
        date_pos = pg_text.find(date) if date_ok else -1
        has_date = date_pos != -1
        has_bal  = bal_m is not None
        if has_date and has_bal:
            return pg_idx, _grid_spills_to_next_page(pg_text, bal_m.start())
        if has_bal and bal_only_match is None:
            bal_only_match = (pg_idx, bal_m.start())
        if has_date and date_only_match is None:
            date_only_match = (pg_idx, date_pos)
    # Balance alone is more specific than date alone (the field known to
    # repeat across sibling accounts) - prefer it when neither page matched
    # both fields together.
    match = bal_only_match if bal_only_match is not None else date_only_match
    if match is None:
        return None, False
    pg_idx, pos = match
    return pg_idx, _grid_spills_to_next_page(page_texts[pg_idx], pos)


_NAVY_BAR_FILL   = (0.0588, 0.2471, 0.4196)
_DPD_ORANGE_FILL = (1.0, 0.5882, 0.3529)
_DPD_RED_FILL    = (0.9804, 0.2353, 0.2353)


def _fill_close(fill, target, tol: float = 0.03) -> bool:
    return fill is not None and all(abs(a - b) < tol for a, b in zip(fill, target))


def _indian_format(n: int) -> str:
    """
    Indian digit-grouping (lakh/crore: 3 digits, then pairs) - e.g. 1243364
    -> "12,43,364". Python's f"{n:,}" produces WESTERN grouping ("1,243,364")
    instead, which never matches CRIF's own printed balance text and made
    page.search_for() silently find nothing (confirmed: this exact mismatch
    made _account_dpd_color_bucket return None - "can't verify" - for every
    account, silently disabling the colour cross-check entirely rather than
    raising, since a signature match failure isn't the caller's fault to see).
    """
    s = str(abs(int(n)))
    if len(s) <= 3:
        return s
    last3, rest = s[-3:], s[:-3]
    parts = []
    while len(rest) > 2:
        parts.insert(0, rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.insert(0, rest)
    return ",".join(parts) + "," + last3


def _band_color_bucket(drawings: list, y0: float, y1: float) -> str:
    """Worst DPD colour ('clean' < 'orange' < 'red') among vector fills whose
    rect top falls within [y0, y1) on one page - see
    _account_dpd_color_bucket for what these buckets mean."""
    bucket = "clean"
    for d in drawings:
        fill = d.get("fill")
        if fill is None:
            continue
        if not (y0 <= d["rect"].y0 < y1):
            continue
        if _fill_close(fill, _DPD_RED_FILL):
            return "red"
        if _fill_close(fill, _DPD_ORANGE_FILL):
            bucket = "orange"
    return bucket


def _account_page_band(doc, pages: list, acc: dict):
    """
    Locate this account's own box on the page(s) it appears on, bounded by
    its navy "Account Information" title bar and the next one (or page
    bottom) - a geometric boundary that's immune to the linear-text reading-
    order scrambling documented in _dpd_span_contaminated and
    _grid_spills_to_next_page, since it comes from the PDF's own vector
    layout, not text order. Returns None if this account's own anchor amount
    (_account_anchor_amount) can't be located on pages[0] at all.

    pages is [primary_page] or [primary_page, next_page] for an account whose
    grid spills across a page break - the continuation page has no header of
    its own to anchor on, so its whole span up to the next account's navy
    title bar (or page bottom) is treated as this account's own continuation,
    only used when the primary page's own band comes back with nothing to
    read past its own navy bar.

    Returns a dict: page0, start, end, drawings0, page1 (or None), end1 (or
    None), drawings1 (or None).
    """
    bal = _account_anchor_amount(acc)
    if bal is None:
        return None
    page0 = doc[pages[0]]
    hits  = page0.search_for(_indian_format(bal))
    if not hits:
        return None
    y_acc     = hits[0].y0
    drawings0 = page0.get_drawings()
    navy_ys   = sorted(d["rect"].y0 for d in drawings0 if _fill_close(d.get("fill"), _NAVY_BAR_FILL))
    start     = max([y for y in navy_ys if y <= y_acc], default=0)
    later     = [y for y in navy_ys if y > y_acc]
    end       = later[0] if later else page0.rect.height
    band = {"page0": page0, "start": start, "end": end, "drawings0": drawings0,
            "page1": None, "end1": None, "drawings1": None}
    if not later and len(pages) > 1:
        page1 = doc[pages[1]]
        drawings1 = page1.get_drawings()
        navy1 = sorted(d["rect"].y0 for d in drawings1 if _fill_close(d.get("fill"), _NAVY_BAR_FILL))
        band["page1"]     = page1
        band["end1"]      = navy1[0] if navy1 else page1.rect.height
        band["drawings1"] = drawings1
    return band


def _account_clip_text(doc, pages: list, acc: dict):
    """
    This account's own text, clipped to its geometric box (_account_page_band)
    instead of trusting the linear-text reading order - the same fix as the
    colour cross-check, applied to text instead of fills. Returns None if the
    account's own anchor can't be located.
    """
    band = _account_page_band(doc, pages, acc)
    if band is None:
        return None
    text = band["page0"].get_text(
        "text", clip=fitz.Rect(0, band["start"], band["page0"].rect.width, band["end"]))
    if band["page1"] is not None:
        text += "\n" + band["page1"].get_text(
            "text", clip=fitz.Rect(0, 0, band["page1"].rect.width, band["end1"]))
    return text


_STATUS_SIDEBAR_X_MAX = 60


def _account_status_geometry(doc, pages: list, acc: dict):
    """
    Read this account's own literal sidebar status tag ("Active"/"Closed", a
    label printed at the far left margin of its box) directly from its
    geometrically-isolated region (_account_page_band), instead of trusting
    crif_parser's text-heuristic _is_closed. Confirmed on a real report:
    _is_closed's Written-off-in-Remarks rule can be tripped by a DIFFERENT,
    contaminated account's remark bleeding into this account's own Remarks
    field - the same reading-order contamination documented for DPD, but
    here with every swallowed Account # masked identically ("xxxx"), which
    _dpd_span_contaminated's distinct-real-code check can't catch at all.
    The corrupted block's own linear text even carried four bare "Active"
    tokens that nothing was checking for.

    Filtering to x0 < 60 is load-bearing, not a tuning knob: it excludes the
    "Closed Date:" FIELD LABEL, which sits mid-row (~x=223 on a standard
    page) in EVERY account's box regardless of true status - an earlier,
    unfiltered version of this same check misread that field label as a
    second-column "Closed" status tag and produced false positives across
    an entire page before the mistake was caught.

    Returns "Active", "Closed", or None if the tag can't be read
    unambiguously (not found, or both words present - the sidebar strip
    itself spilling across a page break the same way a DPD grid can).
    """
    band = _account_page_band(doc, pages, acc)
    if band is None:
        return None
    words0 = band["page0"].get_text("words")
    statuses = {w[4] for w in words0
                if band["start"] <= w[1] < band["end"] and w[4] in ("Active", "Closed")
                and w[0] < _STATUS_SIDEBAR_X_MAX}
    if band["page1"] is not None:
        words1 = band["page1"].get_text("words")
        statuses |= {w[4] for w in words1
                     if 0 <= w[1] < band["end1"] and w[4] in ("Active", "Closed")
                     and w[0] < _STATUS_SIDEBAR_X_MAX}
    return next(iter(statuses)) if len(statuses) == 1 else None


def _geometry_recover_status(accounts: list, doc, page_texts: list) -> list:
    """
    Reads every account's own geometric sidebar tag (_account_status_geometry)
    - CRIF's own literal "Active"/"Closed" printed label - and records it
    verbatim on the account as "status_per_cibil", independent of whatever
    crif_parser's text-heuristic _is_closed concluded. When the two disagree
    and the sidebar read is unambiguous, "status" is also corrected to match
    it - confirmed on a real report to catch 8 accounts wrongly marked Closed
    by contaminated Remarks-field text, all independently verified Active by
    their own sidebar tag.

    "status_per_cibil" is populated for every account this can read (not
    just the ones it corrects) so it can be shown as its own column - an
    analyst can then see both crif_parser's derived status and CRIF's own
    printed tag side by side, including the (rare) cases they still agree.
    Left as None when the tag can't be read unambiguously (not found on this
    account's own anchor, or the sidebar strip itself spills across a page
    break the same way a DPD grid can) - shown as "Check CIBIL", not guessed.

    Digital reports only (doc/page_texts are PyMuPDF's own vector/text
    layer, meaningless once OCR'd to a flat string). Mutates accounts
    in-place; returns the sorted sr_no of every account whose "status" this
    corrected (not merely recorded).
    """
    corrected = []
    for acc in accounts:
        pg, spills = _find_account_page(acc, page_texts)
        if pg is None:
            acc["status_per_cibil"] = None
            continue
        pages = [pg] + ([pg + 1] if spills else [])
        try:
            truth = _account_status_geometry(doc, pages, acc)
        except Exception:
            truth = None
        acc["status_per_cibil"] = truth
        if truth is not None and truth != acc["status"]:
            acc["status"] = truth
            corrected.append(acc["sr_no"])
    return sorted(corrected)


def _account_dpd_color_bucket(doc, pages: list, acc: dict):
    """
    Independent, Gemini-free cross-check on a Vision-read max_dpd: CRIF's own
    report colours a payment-history cell orange for 30 < DPD <= 90 and red
    for DPD > 90 (never for DPD <= 30) - confirmed against five real accounts
    across this report (031->orange, 381->red, and three genuinely
    uncoloured accounts, one of them the exact case where Vision separately
    hallucinated 47, which this catches as inconsistent with 'clean').
    Read directly from the PDF's own vector fill colours (page.get_drawings()),
    not a rendered image, so verifying a Vision answer costs zero extra model
    calls. Returns "clean" (implies DPD <= 30), "orange" (30 < DPD <= 90),
    "red" (DPD > 90), or None if this account's own anchor can't be located
    (see _account_page_band) - callers must not gate on None, since there's
    no basis to accept or reject in that case.
    """
    band = _account_page_band(doc, pages, acc)
    if band is None:
        return None
    bucket = _band_color_bucket(band["drawings0"], band["start"], band["end"])
    if bucket == "clean" and band["page1"] is not None:
        bucket = _band_color_bucket(band["drawings1"], 0, band["end1"])
    return bucket


def _geometry_extract_dpd(doc, pages: list, acc: dict):
    """
    Deterministic recovery of (max_dpd, last_reported_dpd, max_dpd_12mo) for
    an account whose rule-based linear-text reading came back None - clips
    the PDF to this account's own geometric box (_account_clip_text) instead
    of trusting the scrambled/contaminated linear text, then runs the SAME
    regex logic the normal rule-based path uses (pre_scoped=True, since the
    "Payment History" label can land AFTER the grid values in this clipped
    text's own reading order - see crif_parser._extract_max_dpd's note).

    Tried BEFORE any Vision call, not just as a cross-check on one - this is
    the "exhaust determinism first" tier: confirmed on a real report to
    recover 5/5 accounts correctly, including one (true DPD=1) that Vision
    could not read reliably even across repeated attempts. Geometry beats
    both linear-text contamination AND Vision's small-digit legibility
    limits, and costs no model call at all.

    Returns (None, None, None) if the account's own anchor can't be located
    or the clipped text still doesn't resolve - callers should fall through
    to Vision in that case, not treat this as a final answer.
    """
    text = _account_clip_text(doc, pages, acc)
    if text is None:
        return None, None, None
    max_dpd_flat = _crif_extract_max_dpd(text, pre_scoped=True)
    last_reported, max_12mo, max_alltime = _crif_extract_dpd_window(text, pre_scoped=True)
    combined = max((v for v in (max_dpd_flat, max_alltime) if v is not None), default=None)
    return combined, last_reported, max_12mo


def _geometry_recover_dpd(accounts: list, doc, page_texts: list) -> list:
    """
    First-tier DPD recovery for every account whose rule-based reading came
    back None - pure PDF geometry (_geometry_extract_dpd), no Gemini call.
    Mutates accounts in-place; called BEFORE deciding whether Vision is even
    needed, so accounts this resolves never reach the Vision fallback at all
    - the "exhaust determinism before VLM" tier. Returns the sorted sr_no of
    every account it resolved, for the caller's own summary/telemetry.
    """
    recovered = []
    for acc in accounts:
        if acc.get("max_dpd") is not None:
            continue
        pg, spills = _find_account_page(acc, page_texts)
        if pg is None:
            continue
        pages = [pg] + ([pg + 1] if spills else [])
        try:
            max_dpd, last_reported, max_12mo = _geometry_extract_dpd(doc, pages, acc)
        except Exception:
            continue
        if max_dpd is not None:
            acc["max_dpd"]           = max_dpd
            acc["last_reported_dpd"] = last_reported
            acc["max_dpd_12mo"]      = max_12mo
            recovered.append(acc["sr_no"])
    return sorted(recovered)


def _dpd_matches_color(dpd: int, bucket) -> bool:
    """
    True if a Vision-read DPD value is consistent with the report's own
    colour coding for that account's worst cell - False means Vision most
    likely misread the cell (see _account_dpd_color_bucket's docstring).
    bucket=None (anchor not found, can't verify either way) always passes.
    """
    if bucket is None:
        return True
    if bucket == "clean":
        return dpd <= 30
    if bucket == "orange":
        return 30 < dpd <= 90
    if bucket == "red":
        return dpd > 90
    return True


def _enrich_dpd_vision(accounts: list, doc, page_texts: list, api_key: str,
                       on_progress=None) -> dict:
    """
    Shared by CRIF Commercial and CRIF Retail (both call this with their own
    scanned/OCR'd accounts). For scanned PDFs, OCR cannot read text inside
    coloured (orange/red) payment history cells, and each provider's own
    _extract_max_dpd returns None for accounts where it found no readable
    payment-history pattern at all (shown as "Check CIBIL" rather than a
    possibly-wrong 0). This function sends each affected page to Gemini
    Vision and resolves max_dpd on those None accounts. Confident 0-DPD
    reads from OCR are left untouched - only genuinely unread accounts are
    sent. Mutates accounts in-place; for Commercial, called only when
    method != METHOD_VISION (Retail has no equivalent full-account Vision
    fallback to have preempted this).

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
    # needs_next[pg] is True when ANY account grouped on that page has its own
    # grid provably absent there (see _grid_spills_to_next_page) - the whole
    # page's call then also gets the next page's image so Vision has
    # something real to read for that account instead of guessing.
    page_map:   dict[int, list] = {}
    needs_next: dict[int, bool] = {}
    for acc in accounts:
        if acc.get("max_dpd") is None:
            pg, spills = _find_account_page(acc, page_texts)
            if pg is not None:
                page_map.setdefault(pg, []).append(acc)
                needs_next[pg] = needs_next.get(pg, False) or spills

    summary = {"pages_sent": [], "accounts_checked": [], "accounts_patched": []}
    if not page_map:
        return summary

    summary["accounts_checked"] = sorted(acc["sr_no"] for accs in page_map.values() for acc in accs)

    # Render all page images on the main thread first. A single malformed page
    # (corrupt embedded image, bad content stream) must not abort the whole
    # extraction - skip it and leave its accounts unresolved (Check CIBIL)
    # rather than losing every already-validated account to one bad render.
    page_uris  = {}
    pages_sent = set()
    for pg_idx in page_map:
        uris = []
        try:
            uris.append(ocr_extractor._img_data_uri(doc[pg_idx]))
            pages_sent.add(pg_idx + 1)
        except Exception:
            continue
        if needs_next.get(pg_idx) and pg_idx + 1 < len(doc):
            try:
                uris.append(ocr_extractor._img_data_uri(doc[pg_idx + 1]))
                pages_sent.add(pg_idx + 2)
            except Exception:
                pass
        page_uris[pg_idx] = uris
    summary["pages_sent"] = sorted(pages_sent)

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
                pg_idx, dpd_list = fut.result()
            except Exception:
                continue
            # Matched by position (dpd_list[i] answers page_map[pg_idx][i]),
            # not by a date|amount key - sibling accounts (same guarantor
            # obligation split across loans) commonly share both fields, so a
            # content key would silently merge two different accounts' DPD.
            acc_pages = [pg_idx] + ([pg_idx + 1] if needs_next.get(pg_idx) else [])
            for acc, dpd in zip(page_map[pg_idx], dpd_list):
                if dpd is None:
                    continue
                # Cross-check against the report's own colour coding before
                # trusting a Vision read (see _account_dpd_color_bucket) -
                # confirmed necessary on a real report: Vision misread a
                # genuinely uncoloured (<=30) cell as 47, which this catches
                # and rejects instead of shipping a confident wrong number.
                # A None bucket (anchor not found) can't be checked either
                # way and is accepted as before.
                try:
                    bucket = _account_dpd_color_bucket(doc, acc_pages, acc)
                except Exception:
                    bucket = None
                if not _dpd_matches_color(dpd, bucket):
                    continue
                acc["max_dpd"] = dpd
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
    # Snapshot of the rule-based extraction, used below to reattach
    # `ownership` onto the Vision re-extraction if it's adopted (see
    # Finding 2 / _reattach_ownership's docstring) - _VISION_PROMPT doesn't
    # request that field.
    rule_based_accounts = accounts

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
            _reattach_ownership(vis, rule_based_accounts)
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
        # CRIF Commercial reports an entity, not an individual - the header
        # doesn't carry a personal PAN/DOB/phone in the format
        # extract_borrower_identity() looks for, so this comes back all
        # None rather than a wrong guess.
        "identity":               extract_borrower_identity(text),
    }


def parse(pdf_source, api_key: str = None, on_progress=None,
          on_dpd_progress=None, enrich_dpd: bool = False) -> dict:
    """
    Parse a CIBIL PDF (CRIF retail, CRIF Commercial, or TransUnion) and return
    structured data. Scanned PDFs are OCR'd first.

    on_progress(current_page, total_pages) is called during OCR if provided.
    on_dpd_progress(done, total) is called during Vision DPD enrichment
    (CRIF Commercial or Retail, scanned only).
    enrich_dpd: opt-in (default False). Rule-based OCR is always tried first;
    Gemini is never called unless this is True - the UI surfaces
    `vision_fallback_recommended` (CRIF Commercial's full-account re-extract
    only) / `dpd_vision_recommended` (CRIF Commercial and Retail's DPD-only
    patch) in the result so the user can decide whether to re-run with it
    enabled.
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
            # Both CRIF paths populate "analysis" (portfolio-level Credit
            # Analysis section); TU deliberately doesn't - no equivalent
            # loan-type/asset-class or derog-rollup data is derivable from a
            # TU Commercial report's own format. Explicit None, not a missing
            # key, so this reads as intentional rather than an oversight.
            "analysis":          None,
            # TU Commercial's header layout doesn't match the CRIF PAN/DOB/
            # phone label formats extract_borrower_identity() looks for; all
            # three come back None rather than a wrong guess against an
            # unverified format.
            "identity":          extract_borrower_identity(text),
        }

    # ── CRIF Commercial ACE path ──────────────────────────────
    if provider == "crif_commercial":
        return _parse_crif_commercial(text, doc, scanned, page_texts, api_key,
                                      on_dpd_progress=on_dpd_progress,
                                      enrich_dpd=enrich_dpd)

    # ── CRIF retail path ──────────────────────────────────────
    name, score, blocks, accounts, reported = parse_crif(text)
    _renumber(accounts)

    # Geometry-based status correction (digital reports only) - runs BEFORE
    # validation since a corrected status changes active-account counts and
    # the active-only balance sum. See _geometry_recover_status's docstring:
    # this catches accounts _is_closed wrongly closed via contaminated
    # Remarks-field text, independent of and complementary to the DPD-only
    # geometry recovery below.
    if not scanned and page_texts:
        status_corrected = _geometry_recover_status(accounts, doc, page_texts)
    else:
        # Scanned reports have no vector/text layer for this to read at all -
        # "Check CIBIL" (None), not a guess, same convention as every other
        # unreadable field.
        for acc in accounts:
            acc["status_per_cibil"] = None
        status_corrected = []

    extraction_method = METHOD_OCR if scanned else METHOD_RULE_BASED
    # CRIF Retail's Account Summary "Total Amount Overdue" includes Closed
    # accounts (unlike Total Current Balance, which is active-only) - see
    # validate_extraction()'s overdue_scope_all_accounts docstring.
    validation        = validate_extraction(accounts, reported, overdue_scope_all_accounts=True)

    # Stage 2: LLM block-fix
    if not validation["valid"] and api_key:
        # Snapshot of the rule-based extraction before any fallback replaces
        # `accounts` - used below to reattach `ownership` (see Finding 2 /
        # _reattach_ownership's docstring), since neither LLM stage's prompt
        # requests that field.
        rule_based_accounts = accounts
        fixed, ok = _llm_fix_blocks(blocks, accounts, api_key)
        if ok:
            _renumber(fixed)
            _reattach_ownership(fixed, rule_based_accounts)
            v2 = validate_extraction(fixed, reported, overdue_scope_all_accounts=True)
            if v2["valid"]:
                accounts          = fixed
                extraction_method = METHOD_LLM_CORRECTION
                validation        = v2
            else:
                # Stage 3: Full-PDF LLM
                full, ok2 = _llm_full(text, api_key, reported.get("account_count"))
                if ok2 and full:
                    _renumber(full)
                    _reattach_ownership(full, rule_based_accounts)
                    accounts          = full
                    extraction_method = METHOD_LLM_FULL
                    validation        = validate_extraction(accounts, reported, overdue_scope_all_accounts=True)

    # Geometry-first DPD recovery (digital reports only - doc/page_texts are
    # PyMuPDF's own text/vector layer, meaningless once a page has been OCR'd
    # to a flat string). Tries pure PDF geometry before ever considering
    # Vision - see _geometry_recover_dpd's docstring; confirmed on a real
    # report to resolve 5/5 accounts Vision either couldn't read reliably or
    # needed multiple attempts for, at zero model-call cost.
    geometry_dpd_recovered = (
        _geometry_recover_dpd(accounts, doc, page_texts) if not scanned and page_texts else []
    )

    # DPD Vision enrichment - same opt-in mechanism and _enrich_dpd_vision
    # helper as CRIF Commercial (see its docstring). Retail has no
    # full-account Vision fallback (crif_parser's block-splitting/regex
    # extraction is the only extraction path here, unlike Commercial's
    # vision_extract_accounts) - this only patches the one field OCR left
    # unreadable (max_dpd is None) on a badly garbled payment-history grid.
    has_unread_dpd = any(a.get("max_dpd") is None for a in accounts)
    # Digital reports don't get OCR garble, but a distinct failure mode can
    # still null max_dpd: PyMuPDF's plain-text reading order can interleave
    # a dense multi-account page across account boundaries (confirmed on a
    # real report - one block's text span held three other accounts' full
    # header+grid content), which _dpd_span_contaminated proves via two
    # different real Account # codes in one block. That's worth a Vision
    # call even on a digital report since the correct grid genuinely exists
    # on the page image; recommending Vision for every other None on a
    # digital report (e.g. a legitimately blank new-account grid) would
    # just re-read the same blank cells for no gain, so that case stays
    # scanned-only.
    has_contaminated_dpd  = any(a.get("dpd_block_contaminated") for a in accounts)
    dpd_vision_recommended = (
        bool(api_key) and has_unread_dpd and (scanned or has_contaminated_dpd)
    )
    dpd_vision_used         = False
    dpd_vision_summary      = {"pages_sent": [], "accounts_checked": [], "accounts_patched": []}
    if enrich_dpd and dpd_vision_recommended and page_texts:
        dpd_vision_used    = True
        dpd_vision_summary = _enrich_dpd_vision(accounts, doc, page_texts, api_key,
                                                on_progress=on_dpd_progress)
        # A patched max_dpd doesn't move the balance/sanction/overdue checks,
        # but validate_extraction's dpd-vs-overdue contradiction check reads
        # max_dpd directly, so it needs to be recomputed off the final values.
        validation = validate_extraction(accounts, reported, overdue_scope_all_accounts=True)

    return {
        "name":              name,
        "score":             score,
        "accounts":          accounts,
        "extraction_method": extraction_method,
        "validation":        validation,
        "provider":          "crif",
        "tesseract_version": ocr_extractor.tesseract_version() if scanned else None,
        "dpd_vision_recommended": dpd_vision_recommended,
        "dpd_vision_used":        dpd_vision_used,
        "dpd_vision_pages":       dpd_vision_summary["pages_sent"],
        "dpd_vision_checked":     dpd_vision_summary["accounts_checked"],
        "dpd_vision_patched":     dpd_vision_summary["accounts_patched"],
        "dpd_geometry_recovered": geometry_dpd_recovered,
        "status_geometry_corrected": status_corrected,
        "analysis": {
            "credit_profile_summary": crif_credit_profile_summary(accounts),
            "derog_summary":          crif_derog_summary(accounts),
        },
        "identity": extract_borrower_identity(text),
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
