"""
parser.py — AutoCAM CIBIL orchestrator

Detects provider → routes to crif_parser or tu_parser → validates →
LLM fallback (CRIF only) on mismatch.

Public API (unchanged):
    parse(pdf_source, api_key=None) → dict
    debug_blocks(pdf_path)
"""

import re
import fitz  # PyMuPDF

from crif_parser import parse_crif, extract_reported_totals, split_account_blocks
from crif_parser import _is_closed, _extract_balance, _extract_entity
from tu_parser   import parse_transunion

# ─────────────────────────────────────────────────────────────────
# EXTRACTION METHOD LABELS  (imported by app.py)
# ─────────────────────────────────────────────────────────────────
METHOD_RULE_BASED     = "Rule-based extraction"
METHOD_LLM_CORRECTION = "LLM correction used"
METHOD_LLM_FULL       = "Full LLM extraction used"


# ─────────────────────────────────────────────────────────────────
# PDF TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────────

def extract_text(pdf_source) -> str:
    if isinstance(pdf_source, str):
        doc = fitz.open(pdf_source)
    else:
        pdf_source.seek(0)
        doc = fitz.open(stream=pdf_source.read(), filetype="pdf")
    pages = [page.get_text() for page in doc]
    doc.close()
    text = "\n".join(pages)
    return text.replace("\xa0", " ").replace("–", "-").replace("—", "-")


# ─────────────────────────────────────────────────────────────────
# PROVIDER DETECTION
# ─────────────────────────────────────────────────────────────────

def _detect_provider(text: str) -> str:
    sample = text[:5000].lower()
    if ("transunion" in sample or "tu cibil" in sample
            or "cibil msme rank" in sample or "cmr-" in sample
            or "commercial credit information report" in sample):
        return "transunion"
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
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
]


def _llm_invoke(api_key: str, prompt: str) -> str:
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
            return llm.invoke([HumanMessage(content=prompt)]).content
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
        for f in ("sanction_amount", "current_balance", "emi", "overdue", "max_dpd"):
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


def parse(pdf_source, api_key: str = None) -> dict:
    """
    Parse a CIBIL PDF (CRIF or TransUnion) and return structured data.

    Returns dict:
        name, score, accounts, extraction_method, validation, provider
    """
    text     = extract_text(pdf_source)
    provider = _detect_provider(text)

    # ── TransUnion path ───────────────────────────────────────────
    if provider == "transunion":
        name, score, accounts, reported, validation = parse_transunion(text)
        _renumber(accounts)
        return {
            "name":              name,
            "score":             score,
            "accounts":          accounts,
            "extraction_method": METHOD_RULE_BASED,
            "validation":        validation,
            "provider":          "transunion",
        }

    # ── CRIF path ─────────────────────────────────────────────────
    name, score, blocks, accounts, reported = parse_crif(text)
    _renumber(accounts)

    extraction_method = METHOD_RULE_BASED
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
