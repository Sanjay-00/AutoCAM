"""
parser.py — AutoCAM CIBIL orchestrator

Detects provider → routes to crif_parser or tu_parser → validates →
LLM fallback (CRIF only) on mismatch.

Public API (unchanged):
    parse(pdf_source, api_key=None) → dict
    debug_blocks(pdf_path)
"""

import re
import json
import fitz  # PyMuPDF

from crif_parser import parse_crif, extract_reported_totals, split_account_blocks
from crif_parser import _is_closed, _extract_balance, _extract_entity
from crif_commercial_parser import parse_crif_commercial
from tu_parser   import parse_transunion
import ocr_extractor

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


def _normalize_text(text: str) -> str:
    return text.replace("\xa0", " ").replace("–", "-").replace("—", "-")


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
    """Public helper — returns report text (OCR'd if the PDF is scanned)."""
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
    if "commercial ace report" in sample or "perform commercial" in sample:
        return "crif_commercial"
    return "crif"


# ─────────────────────────────────────────────────────────────────
# CRIF VALIDATION
# ─────────────────────────────────────────────────────────────────

def validate_extraction(accounts: list, reported: dict) -> dict:
    """CRIF validation: active count + active balance vs Account Summary."""
    issues  = []
    active  = [a for a in accounts if a.get("status") == "Active"]
    count   = len(active)
    balance = sum(a.get("current_balance", 0) for a in active)

    exp_count = reported.get("account_count")
    exp_bal   = reported.get("total_balance")

    if exp_count is not None and count != exp_count:
        issues.append(
            f"Active account count mismatch: extracted {count}, "
            f"report says {exp_count}"
        )
    if exp_bal and exp_bal > 0:
        if abs(balance - exp_bal) > max(exp_bal * 0.05, 1000):
            issues.append(
                f"Balance mismatch: extracted Rs.{balance:,}, "
                f"report says Rs.{exp_bal:,}"
            )

    return {
        "valid":             len(issues) == 0,
        "issues":            issues,
        "extracted_count":   count,
        "extracted_balance": balance,
        "expected_count":    exp_count,
        "expected_balance":  exp_bal,
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


def _llm_invoke(api_key: str, prompt) -> str:
    """
    Invoke the Gemini model cascade. `prompt` may be a plain string or a
    multimodal content list (text + image_url parts) for Vision extraction.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.messages import HumanMessage
    except ImportError:
        raise RuntimeError("langchain_google_genai not installed")

    for model in _LLM_MODELS:
        try:
            llm = ChatGoogleGenerativeAI(
                model=model, google_api_key=api_key,
                temperature=0.1, max_tokens=8192,
            )
            return _content_to_text(llm.invoke([HumanMessage(content=prompt)]).content)
        except Exception as e:
            if "404" in str(e) or "NOT_FOUND" in str(e):
                continue
            raise
    raise RuntimeError(f"No Gemini model responded. Tried: {_LLM_MODELS}")


def _strip_md(text: str) -> str:
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    return re.sub(r'\s*```$', '', text).strip()


def _normalize(accounts: list) -> list:
    for acc in accounts:
        for f in ("sr_no", "sanction_amount", "current_balance", "emi", "overdue", "max_dpd"):
            try:
                acc[f] = int(float(str(acc.get(f, 0)).replace(",", "")))
            except (ValueError, TypeError):
                acc[f] = 0
        if not acc.get("date_of_sanction"):
            acc["date_of_sanction"] = "NA"
        if acc.get("status", "").lower() not in ("active", "closed"):
            acc["status"] = "Active"
    return accounts


def _llm_fix_blocks(blocks: list, current: list, api_key: str) -> tuple:
    import json
    blocks_text = "\n\n---ACCOUNT---\n\n".join(
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


def _parse_crif_commercial(text, doc, scanned, page_texts, api_key) -> dict:
    """
    CRIF Commercial ACE path. Rule-based on the (possibly OCR'd) text; when the
    source was scanned and the parse fails the report's summary validation, fall
    back to Gemini Vision on the targeted account pages — but only adopt the
    Vision result if it validates at least as well as the OCR result.
    """
    name, score, blocks, accounts, reported = parse_crif_commercial(text)
    _renumber(accounts)

    method     = METHOD_OCR if scanned else METHOD_RULE_BASED
    validation = validate_extraction(accounts, reported)

    if not validation["valid"] and scanned and api_key:
        pages = ocr_extractor.select_pages(page_texts) if page_texts else []
        vis = ocr_extractor.vision_extract_accounts(
            doc, pages, api_key,
            invoke_fn=_llm_invoke, postprocess_fn=_vision_postprocess,
        )
        if vis:
            _renumber(vis)
            v_vis = validate_extraction(vis, reported)
            if _val_quality(v_vis) > _val_quality(validation):
                accounts, validation, method = vis, v_vis, METHOD_VISION

    return {
        "name":              name,
        "score":             score,
        "accounts":          accounts,
        "extraction_method": method,
        "validation":        validation,
        "provider":          "crif_commercial",
    }


def parse(pdf_source, api_key: str = None, on_progress=None) -> dict:
    """
    Parse a CIBIL PDF (CRIF retail, CRIF Commercial, or TransUnion) and return
    structured data. Scanned PDFs are OCR'd first.

    on_progress(current_page, total_pages) is called during OCR if provided.

    Returns dict:
        name, score, accounts, extraction_method, validation, provider
    """
    doc = _open_doc(pdf_source)
    try:
        text, scanned, page_texts = _extract(doc, on_progress=on_progress)
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
            }

        # ── CRIF Commercial ACE path ──────────────────────────────
        if provider == "crif_commercial":
            return _parse_crif_commercial(text, doc, scanned, page_texts, api_key)

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
        }
    finally:
        doc.close()


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
    print(f"--- All 'Account Information' occurrences ({len(all_raw)}) ---")
    for hit in all_raw:
        print(f"  {hit.replace(chr(10), '↵ ')[:80]}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parser.py <path_to_pdf>")
    else:
        debug_blocks(sys.argv[1])
