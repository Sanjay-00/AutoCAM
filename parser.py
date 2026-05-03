"""
parser.py — Production-grade CRIF High Mark CIBIL PDF parser

Extraction pipeline:
  Stage 1  Rule-based  (primary, PyMuPDF blocks + regex)
  Stage 2  LLM block-fix fallback   (Gemini, if count/balance mismatch)
  Stage 3  Full-PDF LLM extraction  (last resort)
"""

import re
import fitz  # PyMuPDF

# ─────────────────────────────────────────────────────────────────
# EXTRACTION METHOD LABELS (used by UI)
# ─────────────────────────────────────────────────────────────────
METHOD_RULE_BASED      = "Rule-based extraction"
METHOD_LLM_CORRECTION  = "LLM correction used"
METHOD_LLM_FULL        = "Full LLM extraction used"


# ─────────────────────────────────────────────────────────────────
# 1. PDF TEXT EXTRACTION
# ─────────────────────────────────────────────────────────────────

def extract_text(pdf_source) -> str:
    """
    Extract full text from PDF using plain text mode.
    get_text() preserves the natural line structure that CRIF patterns rely on.
    get_text("blocks") strips trailing newlines per block, breaking patterns
    like 'Account Information\\n3\\n' that worked reliably with plain mode.
    """
    if isinstance(pdf_source, str):
        doc = fitz.open(pdf_source)
    else:
        pdf_source.seek(0)
        doc = fitz.open(stream=pdf_source.read(), filetype="pdf")
    pages = [page.get_text() for page in doc]
    doc.close()
    text = "\n".join(pages)
    text = text.replace("\xa0", " ").replace("–", "-").replace("—", "-")
    return text


# ─────────────────────────────────────────────────────────────────
# 2. BORROWER NAME & SCORE
# ─────────────────────────────────────────────────────────────────

def _extract_name(text: str) -> str:
    m = (
        re.search(r'For\s+([A-Z][A-Z\s]+?)\s*\n', text)
        or re.search(r'For\s+([A-Z][A-Z\s]+?)\s+(?:CHM|Application|Credit)', text)
    )
    if not m:
        return "Unknown"
    raw = m.group(1).strip().split()
    seen, words = set(), []
    for w in raw:
        if w not in seen:
            seen.add(w)
            words.append(w)
    return " ".join(words)


def _extract_score(text: str):
    # CRIF PERFORM: 3-digit integer after "300-900"
    m = re.search(r'300-900\s*\n?\s*(\d{3})\b', text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    m = re.search(r'PERFORM[^\n]*?(\d{3})\b', text)
    if m:
        try:
            v = int(m.group(1))
            if 300 <= v <= 900:
                return v
        except ValueError:
            pass
    return "NA"


# ─────────────────────────────────────────────────────────────────
# 3. REPORTED TOTALS (for validation)
# ─────────────────────────────────────────────────────────────────

def _extract_reported_totals(text: str) -> dict:
    """
    Parse the CRIF Account Summary table to get:
      - active_accounts  (column index 1 in the 12-column table)
      - total_current_balance  (column index 6)

    CRIF table column order (fixed):
      0  Number of Accounts
      1  Active Accounts          ← validate against
      2  Overdue Accounts
      3  Secured Accounts
      4  UnSecured Accounts
      5  Untagged Accounts
      6  Total Current Balance    ← validate against
      7  Current Balance Secured
      8  Current Balance Unsecured
      9  Total Sanctioned Amount
      10 Total Disbursed Amount
      11 Total Amount Overdue

    PyMuPDF splits table cells into individual blocks, so each header word
    appears on its own line.  The 12 values appear in column order AFTER the
    last header keyword "Total Amount Overdue".
    """
    totals = {"account_count": None, "total_balance": None}

    # Find Account Summary section
    summary_m = re.search(r'Account\s+Summary\b', text, re.IGNORECASE)
    if not summary_m:
        return totals

    # Window: Account Summary → first Account Information block (or +3000 chars)
    sec_start = summary_m.start()
    ai_m = re.search(r'Account\s+Information\s*\n\s*\d', text[sec_start:])
    sec_end = sec_start + (ai_m.start() if ai_m else 3000)
    section = text[sec_start:sec_end]

    # Primary strategy: locate last column header "Total Amount Overdue",
    # then read the 12 values that follow in column order.
    last_hdr_m = re.search(
        r'Total\s*[\s\n]*Amount\s*[\s\n]*Overdue', section, re.IGNORECASE
    )
    if last_hdr_m:
        after = section[last_hdr_m.end():]
        # Extract all numbers (handles Indian comma format like 1,23,456)
        raw_nums = re.findall(r'\b(\d{1,3}(?:,\d{2,3})*|\d+)\b', after)
        nums = [_to_int(n) for n in raw_nums]
        if len(nums) >= 7:
            totals["account_count"] = nums[1]   # col 1 = Active Accounts
            totals["total_balance"]  = nums[6]   # col 6 = Total Current Balance
            return totals
        if len(nums) >= 2:
            totals["account_count"] = nums[1]
            return totals

    # Fallback: direct label search (handles alternate CRIF layouts)
    for pat in (
        r'Active\s+Accounts\s*:?\s*(\d+)',
        r'Active\s*\n\s*Accounts\s*\n\s*(\d+)',
    ):
        m = re.search(pat, section, re.IGNORECASE)
        if m:
            totals["account_count"] = int(m.group(1))
            break

    for pat in (
        r'Total\s+Current\s+Balance\s*:?\s*([\d,]+)',
        r'Total\s*\n?\s*Current\s*\n?\s*Balance\s*\n\s*([\d,]+)',
    ):
        m = re.search(pat, section, re.IGNORECASE)
        if m:
            totals["total_balance"] = _to_int(m.group(1))
            break

    return totals


# ─────────────────────────────────────────────────────────────────
# 4. ACCOUNT BLOCK SPLITTING  (most critical step)
# ─────────────────────────────────────────────────────────────────

_BLOCK_PATTERNS = [
    # P1: number on its own line  — "Account Information\n3\n"
    re.compile(r'Account\s+Information\s*\n\s*(\d{1,3})\s*\n', re.MULTILINE),
    # P2: blank line before number — "Account Information\n\n3\n"
    re.compile(r'Account\s+Information\s*\n\s*\n\s*(\d{1,3})\s*\n', re.MULTILINE),
    # P3: number on same line     — "Account Information 3\n"
    re.compile(r'Account\s+Information\s+(\d{1,3})\s*\n', re.MULTILINE),
]

# Matches the bare "Account Information\n" header (no dash — excludes
# Appendix entries like "Account Information - Account #")
_AI_HEADER = re.compile(r'Account\s+Information\s*\n', re.MULTILINE)

# Confirms a position is inside a real account block (not a summary table)
_ACCOUNT_FIELD = re.compile(
    r'Account\s+Type:|Disbursed\s+Date:|Current\s+Balance:|Credit\s+Grantor:',
    re.IGNORECASE,
)


def split_account_blocks(text: str) -> list:
    """
    Split full PDF text into individual account blocks.

    Two-pass approach:
      Pass 1  — P1/P2/P3 patterns capture blocks whose account number is
                present in the text (standard case).
      Pass 2  — Scans for any 'Account Information' header that was NOT
                captured in Pass 1.  This handles the HTML-to-PDF page-break
                case where the browser's print header (timestamp, filename,
                page number) lands between 'Account Information' and the
                account number, causing the number to disappear entirely.
                The missing account number is inferred from its ordinal
                position between the surrounding numbered accounts.

    Returns list of (account_number: int, block_text: str) tuples.
    """
    # ── Pass 1: numbered blocks ───────────────────────────────────
    candidates = []
    for pat in _BLOCK_PATTERNS:
        for m in pat.finditer(text):
            candidates.append((m.start(), int(m.group(1))))

    if not candidates:
        return []

    candidates.sort(key=lambda x: x[0])
    deduped = [candidates[0]]
    for pos, num in candidates[1:]:
        if pos - deduped[-1][0] > 30:
            deduped.append((pos, num))

    # ── Pass 2: recover page-break victims (number swallowed by header) ──
    found_positions = {pos for pos, _ in deduped}

    for m in _AI_HEADER.finditer(text):
        pos = m.start()
        # Skip if already captured by Pass 1
        if any(abs(pos - fp) < 50 for fp in found_positions):
            continue
        # Confirm it's a real account block, not an Appendix/Summary entry
        if not _ACCOUNT_FIELD.search(text[pos: pos + 1000]):
            continue
        # Infer account number from position between neighbours
        prev_nums = [n for p, n in deduped if p < pos]
        prev_num  = max(prev_nums) if prev_nums else 0
        deduped.append((pos, prev_num + 1))
        found_positions.add(pos)

    # ── Build blocks ──────────────────────────────────────────────
    deduped.sort(key=lambda x: x[0])
    blocks = []
    for i, (start_pos, acct_num) in enumerate(deduped):
        end_pos = deduped[i + 1][0] if i + 1 < len(deduped) else len(text)
        blocks.append((acct_num, text[start_pos:end_pos]))

    return blocks


# ─────────────────────────────────────────────────────────────────
# 5. FIELD EXTRACTION HELPERS
# ─────────────────────────────────────────────────────────────────

def _to_int(s) -> int:
    try:
        return int(float(str(s).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def _next_line_value(block: str, label: str) -> str:
    """Return text on the line immediately after `label`."""
    m = re.search(re.escape(label) + r'\s*\n\s*([^\n]+)', block)
    return m.group(1).strip() if m else ""


def _inline_value(block: str, label: str) -> str:
    """Return text after `label` on the same line."""
    m = re.search(re.escape(label) + r'[\s:]+([\S][^\n]*)', block)
    return m.group(1).strip() if m else ""


def _extract_date(block: str) -> str:
    val = _next_line_value(block, "Disbursed Date:")
    if re.match(r'\d{2}-\d{2}-\d{4}', val):
        return val
    m = re.search(r'Disbursed\s+Date[:\s]+(\d{2}-\d{2}-\d{4})', block)
    if m:
        return m.group(1)
    m = re.search(r'Date\s+of\s+Sanction[:\s]+(\d{2}[-/]\d{2}[-/]\d{4})', block, re.IGNORECASE)
    if m:
        return m.group(1).replace("/", "-")
    return "NA"


def _extract_sanction_amt(block: str) -> int:
    val = _next_line_value(block, "Disbd Amt/High Credit:")
    if re.match(r'[\d,]+', val):
        return _to_int(val.split()[0])
    m = re.search(r'Disbd\s+Amt[/\s](?:High\s+Credit)?[:\s]*([\d,]+)', block, re.IGNORECASE)
    if m:
        return _to_int(m.group(1))
    return 0


def _extract_balance(block: str) -> int:
    val = _next_line_value(block, "Current Balance:")
    if re.match(r'[\d,]+', val):
        return _to_int(val.split()[0])
    m = re.search(r'Current\s+Balance[:\s]*([\d,]+)', block, re.IGNORECASE)
    return _to_int(m.group(1)) if m else 0


def _extract_overdue(block: str) -> int:
    val = _next_line_value(block, "Overdue Amt:")
    if re.match(r'[\d,]+', val):
        return _to_int(val.split()[0])
    m = re.search(r'Overdue\s+(?:Amt)?[:\s]*([\d,]+)', block, re.IGNORECASE)
    return _to_int(m.group(1)) if m else 0


def _extract_emi(block: str) -> int:
    # InstlAmt/Freq: <amount>/<frequency>
    val = _next_line_value(block, "InstlAmt/Freq:")
    m = re.match(r'([\d,]+)/', val)
    if m:
        return _to_int(m.group(1))
    m = re.search(r'InstlAmt/Freq[:\s]*([\d,]+)/', block)
    return _to_int(m.group(1)) if m else 0


def _extract_entity(block: str) -> str:
    val = _next_line_value(block, "Credit Grantor:")
    if not val or val.upper().startswith("XXXX"):
        m = re.search(r'Credit\s+Grantor[:\s]+([^\n\t]+)', block)
        val = m.group(1).strip() if m else ""
    if val and not val.upper().startswith("XXXX"):
        for stop in ("Account #", "Lender Type", "Account Type"):
            if stop in val:
                val = val[: val.index(stop)].strip()
        return val or "Not Disclosed"
    return "Not Disclosed"


def _extract_loan_type(block: str) -> str:
    val = _next_line_value(block, "Account Type:")
    if not val:
        m = re.search(r'Account\s+Type[:\s]+([^\n\t]+)', block)
        val = m.group(1).strip() if m else ""
    for stop in ("Credit Grantor", "Account #", "Lender Type"):
        if stop in val:
            val = val[: val.index(stop)].strip()
    return val or "Unknown"


def _extract_max_dpd(block: str) -> int:
    # CRIF DPD pattern: NNN/STD, NNN/SMA, NNN/SUB, NNN/DBT, NNN/LOS, NNN/XXX
    vals = [
        int(m.group(1))
        for m in re.finditer(r'(\d{3})/(?:XXX|STD|SMA|SUB|DBT|LOS)', block)
        if int(m.group(1)) < 998  # 999 = data not reported
    ]
    return max(vals) if vals else 0


def _is_closed(block: str) -> bool:
    """
    Three rules — any one is sufficient:

    Rule 1  Closed Date field contains a valid date.

    Rule 2  Remarks field contains 'Written-off' (or variants).
            In CRIF PDFs the label is split across two lines:
              "Account\\nRemarks:\\nWritten-off"
            so we search for 'Remarks:' and read the next line.

    Rule 3  Compact block — 'Closed' appears on its own line immediately
            after the account number, before any field labels.
            Seen in short/history-only blocks (e.g. Account 12 pattern).
    """
    # ── Rule 1: Closed Date ───────────────────────────────────────
    val = _next_line_value(block, "Closed Date:")
    if val and re.match(r'\d{2}-\d{2}-\d{4}', val):
        return True
    m = re.search(r'Closed\s+Date\s*:\s*(\S+)', block)
    if m and re.match(r'\d{2}-\d{2}-\d{4}', m.group(1)):
        return True

    # ── Rule 2: Written-off in Remarks ───────────────────────────
    rem_m = re.search(r'Remarks\s*:\s*\n\s*([^\n]+)', block)
    if rem_m and re.search(r'written.?off', rem_m.group(1), re.IGNORECASE):
        return True

    # ── Rule 3: Compact block — 'Closed' before first field label ─
    first_field = re.search(
        r'(?:Ownership|Disbursed Date|Current Balance|Closed Date|Account Type)\s*:',
        block,
    )
    header_region = block[: first_field.start()] if first_field else block[:300]
    if re.search(r'\nClosed\n', header_region):
        return True

    return False


def _build_positional_lists(text: str) -> tuple:
    """
    Build ordered Account Type and Credit Grantor lists by scanning the full text.

    In CRIF PDFs, these labels appear in a compact summary table BEFORE the
    detailed blocks, in the same ordinal order as the account blocks.  Indexing
    by position is more reliable than per-block extraction because the summary
    table labels are always clean, while the detailed block labels can have
    concatenated or truncated text.
    """
    at_re = re.compile(r'Account Type:\s*\n?\s*(.+?)(?:\n|$)', re.MULTILINE)
    at_list = []
    for m in at_re.finditer(text):
        raw = m.group(1).strip()
        for stop in ("Credit Grantor", "Account #", "Lender Type"):
            if stop in raw:
                raw = raw[:raw.index(stop)].strip()
        if raw:
            at_list.append(raw)

    cg_re = re.compile(r'Credit Grantor:\s*\n?\s*(.+?)(?:\n|$)', re.MULTILINE)
    cg_list = []
    for m in cg_re.finditer(text):
        raw = m.group(1).strip()
        for stop in ("Account #", "Lender Type"):
            if stop in raw:
                raw = raw[:raw.index(stop)].strip()
        entity = raw.strip()
        if entity.upper().startswith("XXXX") or entity == "":
            entity = "Not Disclosed"
        cg_list.append(entity)

    return at_list, cg_list


def extract_account(acct_num: int, block: str,
                    loan_type: str = None, entity: str = None) -> dict:
    return {
        "sr_no":            acct_num,
        "date_of_sanction": _extract_date(block),
        "sanction_amount":  _extract_sanction_amt(block),
        "current_balance":  _extract_balance(block),
        "emi":              _extract_emi(block),
        "overdue":          _extract_overdue(block),
        "entity":           entity if entity else _extract_entity(block),
        "type_of_loan":     loan_type if loan_type else _extract_loan_type(block),
        "max_dpd":          _extract_max_dpd(block),
        "status":           "Closed" if _is_closed(block) else "Active",
    }


# ─────────────────────────────────────────────────────────────────
# 6. VALIDATION
# ─────────────────────────────────────────────────────────────────

def validate_extraction(accounts: list, reported: dict) -> dict:
    """
    Validate against CRIF Account Summary table values.

    CRIF tip: "Current Balance & Disbursed Amount is considered ONLY for
    ACTIVE accounts."  So we compare:
      - extracted active count  vs  reported Active Accounts (col 1)
      - sum of active balances  vs  reported Total Current Balance (col 6)
    """
    issues = []
    active = [a for a in accounts if a.get("status") == "Active"]

    extracted_count   = len(active)
    extracted_balance = sum(a.get("current_balance", 0) for a in active)

    expected_count   = reported.get("account_count")   # Active Accounts
    expected_balance = reported.get("total_balance")    # Total Current Balance

    if expected_count is not None and extracted_count != expected_count:
        issues.append(
            f"Active account count mismatch: extracted {extracted_count}, "
            f"report says {expected_count}"
        )

    if expected_balance and expected_balance > 0:
        tolerance = max(expected_balance * 0.05, 1000)  # 5% or ₹1,000 floor
        if abs(extracted_balance - expected_balance) > tolerance:
            issues.append(
                f"Balance mismatch: extracted Rs.{extracted_balance:,}, "
                f"report says Rs.{expected_balance:,}"
            )

    return {
        "valid":             len(issues) == 0,
        "issues":            issues,
        "extracted_count":   extracted_count,        # active count
        "extracted_balance": extracted_balance,       # active balance sum
        "expected_count":    expected_count,
        "expected_balance":  expected_balance,
    }




# ─────────────────────────────────────────────────────────────────
# 7. LLM FALLBACK HELPERS
# ─────────────────────────────────────────────────────────────────

_LLM_MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
]


def _strip_markdown(text: str) -> str:
    text = re.sub(r'^```(?:json)?\s*', '', text.strip())
    return re.sub(r'\s*```$', '', text).strip()


def _normalize_accounts(accounts: list) -> list:
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


def _llm_invoke(api_key: str, prompt: str) -> str:
    """Try each Gemini model in sequence; return first successful response."""
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


def _llm_fix_blocks(
    blocks: list, current_accounts: list, api_key: str
) -> tuple:
    """
    Fallback 1: Send raw account blocks + current extraction to LLM.
    LLM corrects field-level errors. Account count stays the same.
    Returns (accounts, success: bool).
    """
    import json

    blocks_text = "\n\n---ACCOUNT---\n\n".join(
        f"ACCOUNT {num}:\n{blk[:1800]}" for num, blk in blocks
    )
    prompt = f"""You are a financial data extraction expert correcting CIBIL account data.
The rule-based extraction below has validation errors. Fix only the wrong fields.

CURRENT EXTRACTION:
{json.dumps(current_accounts, indent=2)}

RAW ACCOUNT BLOCKS FROM PDF:
{blocks_text[:14000]}

Return ONLY a valid JSON array. Each object must have exactly these keys:
sr_no (int), date_of_sanction (str DD-MM-YYYY or "NA"), sanction_amount (int),
current_balance (int), emi (int), overdue (int), entity (str), type_of_loan (str),
max_dpd (int), status ("Active" or "Closed").
Do NOT add or remove accounts."""

    try:
        raw      = _llm_invoke(api_key, prompt)
        fixed    = __import__("json").loads(_strip_markdown(raw))
        if isinstance(fixed, list) and fixed:
            return _normalize_accounts(fixed), True
    except Exception:
        pass
    return current_accounts, False


def _llm_full_extraction(text: str, api_key: str, expected_count) -> tuple:
    """
    Fallback 2: Full PDF text → LLM. Last resort when block correction failed.
    Returns (accounts, success: bool).
    """
    hint = f"There should be {expected_count} accounts." if expected_count else ""
    prompt = f"""Extract ALL loan accounts from this CIBIL credit report. {hint}

Return ONLY a valid JSON array. Each element must have:
sr_no (int), date_of_sanction ("DD-MM-YYYY" or "NA"), sanction_amount (int),
current_balance (int), emi (int), overdue (int), entity (str),
type_of_loan (str), max_dpd (int), status ("Active" or "Closed")

CIBIL TEXT:
{text[:28000]}"""

    try:
        raw      = _llm_invoke(api_key, prompt)
        accounts = __import__("json").loads(_strip_markdown(raw))
        if isinstance(accounts, list) and accounts:
            return _normalize_accounts(accounts), True
    except Exception:
        pass
    return [], False


# ─────────────────────────────────────────────────────────────────
# 8. MAIN PARSE FUNCTION
# ─────────────────────────────────────────────────────────────────

def parse(pdf_source, api_key: str = None) -> dict:
    """
    Parse a CRIF CIBIL PDF and return structured data.

    Parameters
    ----------
    pdf_source : str | file-like
        Path or seekable file object for the PDF.
    api_key : str, optional
        Gemini API key. Required to activate LLM fallback stages.

    Returns
    -------
    dict with keys:
        name               str
        score              int | "NA"
        accounts           list[dict]   — all accounts (active + closed)
        extraction_method  str          — one of METHOD_* constants
        validation         dict         — count/balance validation result
    """
    text      = extract_text(pdf_source)
    name      = _extract_name(text)
    score     = _extract_score(text)
    reported  = _extract_reported_totals(text)

    # ── Stage 1: Rule-based ───────────────────────────────────────
    blocks           = split_account_blocks(text)
    at_list, cg_list = _build_positional_lists(text)

    accounts = []
    for idx, (num, blk) in enumerate(blocks):
        loan_type = at_list[idx] if idx < len(at_list) else None
        entity    = cg_list[idx] if idx < len(cg_list) else None
        accounts.append(extract_account(num, blk, loan_type, entity))

    accounts.sort(key=lambda x: x["sr_no"])
    for i, acc in enumerate(accounts, 1):
        acc["sr_no"] = i

    extraction_method = METHOD_RULE_BASED
    validation        = validate_extraction(accounts, reported)

    # ── Stage 2: LLM block-fix (only on mismatch + key available) ─
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
                # ── Stage 3: Full-PDF LLM (last resort) ──────────
                full, ok2 = _llm_full_extraction(
                    text, api_key, reported.get("account_count")
                )
                if ok2 and full:
                    _renumber(full)
                    accounts          = full
                    extraction_method = METHOD_LLM_FULL
                    validation        = validate_extraction(accounts, reported)

    return {
        "name":             name,
        "score":            score,
        "accounts":         accounts,
        "extraction_method": extraction_method,
        "validation":       validation,
    }


def _renumber(accounts: list) -> None:
    accounts.sort(key=lambda x: x.get("sr_no", 0))
    for i, acc in enumerate(accounts, 1):
        acc["sr_no"] = i


# ─────────────────────────────────────────────────────────────────
# DEBUG UTILITY  (run directly: python parser.py <pdf_path>)
# ─────────────────────────────────────────────────────────────────

def debug_blocks(pdf_path: str) -> None:
    """
    Print a diagnostic report showing exactly which account blocks were
    found and the raw text around each block header.

    Usage:
        python parser.py your_failing_report.pdf
    """
    text   = extract_text(pdf_path)
    blocks = split_account_blocks(text)
    rep    = _extract_reported_totals(text)

    print(f"\n{'='*60}")
    print(f"  Blocks found   : {len(blocks)}")
    print(f"  Expected active: {rep.get('account_count', 'not found in PDF')}")
    print(f"  Expected balance: {rep.get('total_balance', 'not found in PDF')}")
    print(f"{'='*60}\n")

    for acct_num, block in blocks:
        header = block[:300].replace('\n', '↵ ')
        status = "CLOSED" if _is_closed(block) else "active"
        bal    = _extract_balance(block)
        entity = _extract_entity(block)
        print(f"  [{acct_num:>3}] {status:<8}  bal={bal:>12,}  entity={entity}")
        print(f"         raw: {header[:120]}")
        print()

    # Show raw text around any Account Information that was NOT captured
    import re as _re
    all_raw = _re.findall(
        r'Account\s+Information[\s\S]{0,60}', text
    )
    found_nums = {n for n, _ in blocks}
    print(f"--- All 'Account Information' occurrences in text ({len(all_raw)}) ---")
    for hit in all_raw:
        short = hit.replace('\n', '↵ ')[:80]
        print(f"  {short}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parser.py <path_to_pdf>")
    else:
        debug_blocks(sys.argv[1])
