"""
crif_parser.py — CRIF High Mark CIBIL parser
Rule-based extraction: block splitting, field extraction, closed detection.
"""

import re


# ─────────────────────────────────────────────────────────────────
# SHARED UTILITY
# ─────────────────────────────────────────────────────────────────

def to_int(s) -> int:
    try:
        return int(float(str(s).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


# ─────────────────────────────────────────────────────────────────
# NAME & SCORE
# ─────────────────────────────────────────────────────────────────

def extract_name(text: str) -> str:
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


def extract_score(text: str):
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
# VALIDATION TOTALS  (from Account Summary table)
# ─────────────────────────────────────────────────────────────────

def extract_reported_totals(text: str) -> dict:
    """
    Parse the CRIF Account Summary 12-column table.
    Active Accounts = col 1, Total Current Balance = col 6.
    """
    totals = {"account_count": None, "total_balance": None}

    summary_m = re.search(r'Account\s+Summary\b', text, re.IGNORECASE)
    if not summary_m:
        return totals

    sec_start = summary_m.start()
    ai_m      = re.search(r'Account\s+Information\s*\n\s*\d', text[sec_start:])
    sec_end   = sec_start + (ai_m.start() if ai_m else 3000)
    section   = text[sec_start:sec_end]

    last_hdr_m = re.search(
        r'Total\s*[\s\n]*Amount\s*[\s\n]*Overdue', section, re.IGNORECASE
    )
    if last_hdr_m:
        after    = section[last_hdr_m.end():]
        raw_nums = re.findall(r'\b(\d{1,3}(?:,\d{2,3})*|\d+)\b', after)
        nums     = [to_int(n) for n in raw_nums]
        if len(nums) >= 7:
            totals["account_count"] = nums[1]
            totals["total_balance"]  = nums[6]
            return totals
        if len(nums) >= 2:
            totals["account_count"] = nums[1]
            return totals

    for pat in (r'Active\s+Accounts\s*:?\s*(\d+)',
                r'Active\s*\n\s*Accounts\s*\n\s*(\d+)'):
        m = re.search(pat, section, re.IGNORECASE)
        if m:
            totals["account_count"] = int(m.group(1))
            break

    for pat in (r'Total\s+Current\s+Balance\s*:?\s*([\d,]+)',
                r'Total\s*\n?\s*Current\s*\n?\s*Balance\s*\n\s*([\d,]+)'):
        m = re.search(pat, section, re.IGNORECASE)
        if m:
            totals["total_balance"] = to_int(m.group(1))
            break

    return totals


# ─────────────────────────────────────────────────────────────────
# ACCOUNT BLOCK SPLITTING
# ─────────────────────────────────────────────────────────────────

_BLOCK_PATTERNS = [
    re.compile(r'Account\s+Information\s*\n\s*(\d{1,3})\s*\n',    re.MULTILINE),
    re.compile(r'Account\s+Information\s*\n\s*\n\s*(\d{1,3})\s*\n', re.MULTILINE),
    re.compile(r'Account\s+Information\s+(\d{1,3})\s*\n',          re.MULTILINE),
]

_AI_HEADER    = re.compile(r'Account\s+Information\s*\n', re.MULTILINE)
_BLOCK_FIELD  = re.compile(
    r'Account\s+Type:|Disbursed\s+Date:|Current\s+Balance:|Credit\s+Grantor:',
    re.IGNORECASE,
)


def split_account_blocks(text: str) -> list:
    """
    Two-pass splitter:
      Pass 1 — P1/P2/P3 patterns (numbered blocks, standard format).
      Pass 2 — recovers blocks whose number was swallowed by a browser
               print header on page breaks (HTML-to-PDF); number inferred
               from ordinal position.
    Returns list of (account_number: int, block_text: str).
    """
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

    found_positions = {pos for pos, _ in deduped}
    for m in _AI_HEADER.finditer(text):
        pos = m.start()
        if any(abs(pos - fp) < 50 for fp in found_positions):
            continue
        if not _BLOCK_FIELD.search(text[pos: pos + 1000]):
            continue
        prev_nums = [n for p, n in deduped if p < pos]
        prev_num  = max(prev_nums) if prev_nums else 0
        deduped.append((pos, prev_num + 1))
        found_positions.add(pos)

    deduped.sort(key=lambda x: x[0])
    blocks = []
    for i, (start_pos, acct_num) in enumerate(deduped):
        end_pos = deduped[i + 1][0] if i + 1 < len(deduped) else len(text)
        blocks.append((acct_num, text[start_pos:end_pos]))

    return blocks


# ─────────────────────────────────────────────────────────────────
# FIELD EXTRACTION
# ─────────────────────────────────────────────────────────────────

def _next_line_value(block: str, label: str) -> str:
    m = re.search(re.escape(label) + r'\s*\n\s*([^\n]+)', block)
    return m.group(1).strip() if m else ""


def _extract_date(block: str) -> str:
    val = _next_line_value(block, "Disbursed Date:")
    if re.match(r'\d{2}-\d{2}-\d{4}', val):
        return val
    m = re.search(r'Disbursed\s+Date[:\s]+(\d{2}-\d{2}-\d{4})', block)
    if m:
        return m.group(1)
    m = re.search(r'Date\s+of\s+Sanction[:\s]+(\d{2}[-/]\d{2}[-/]\d{4})', block, re.IGNORECASE)
    return m.group(1).replace("/", "-") if m else "NA"


def _extract_sanction_amt(block: str) -> int:
    val = _next_line_value(block, "Disbd Amt/High Credit:")
    if re.match(r'[\d,]+', val):
        return to_int(val.split()[0])
    m = re.search(r'Disbd\s+Amt[/\s](?:High\s+Credit)?[:\s]*([\d,]+)', block, re.IGNORECASE)
    return to_int(m.group(1)) if m else 0


def _extract_balance(block: str) -> int:
    val = _next_line_value(block, "Current Balance:")
    if re.match(r'-?[\d,]+', val):
        return to_int(val.split()[0])
    m = re.search(r'Current\s+Balance[:\s]*(-?[\d,]+)', block, re.IGNORECASE)
    return to_int(m.group(1)) if m else 0


def _extract_overdue(block: str) -> int:
    val = _next_line_value(block, "Overdue Amt:")
    if re.match(r'[\d,]+', val):
        return to_int(val.split()[0])
    m = re.search(r'Overdue\s+(?:Amt)?[:\s]*([\d,]+)', block, re.IGNORECASE)
    return to_int(m.group(1)) if m else 0


def _extract_emi(block: str) -> int:
    val = _next_line_value(block, "InstlAmt/Freq:")
    m = re.match(r'([\d,]+)/', val)
    if m:
        return to_int(m.group(1))
    m = re.search(r'InstlAmt/Freq[:\s]*([\d,]+)/', block)
    return to_int(m.group(1)) if m else 0


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
    vals = [
        int(m.group(1))
        for m in re.finditer(r'(\d{3})/(?:XXX|STD|SMA|SUB|DBT|LOS)', block)
        if int(m.group(1)) < 998
    ]
    return max(vals) if vals else 0


def _is_closed(block: str) -> bool:
    """
    Rule 1: Closed Date has a valid date.
    Rule 2: Remarks contains 'Written-off'.
    Rule 3: Compact block — 'Closed' before any field label.
    """
    val = _next_line_value(block, "Closed Date:")
    if val and re.match(r'\d{2}-\d{2}-\d{4}', val):
        return True
    m = re.search(r'Closed\s+Date\s*:\s*(\S+)', block)
    if m and re.match(r'\d{2}-\d{2}-\d{4}', m.group(1)):
        return True

    rem_m = re.search(r'Remarks\s*:\s*\n\s*([^\n]+)', block)
    if rem_m and re.search(r'written.?off', rem_m.group(1), re.IGNORECASE):
        return True

    first_field = re.search(
        r'(?:Ownership|Disbursed Date|Current Balance|Closed Date|Account Type)\s*:',
        block,
    )
    header_region = block[: first_field.start()] if first_field else block[:300]
    if re.search(r'\nClosed\n', header_region):
        return True

    return False


# ─────────────────────────────────────────────────────────────────
# POSITIONAL LISTS  (entity + loan type from compact summary table)
# ─────────────────────────────────────────────────────────────────

def build_positional_lists(text: str) -> tuple:
    at_re   = re.compile(r'Account Type:\s*\n?\s*(.+?)(?:\n|$)', re.MULTILINE)
    at_list = []
    for m in at_re.finditer(text):
        raw = m.group(1).strip()
        for stop in ("Credit Grantor", "Account #", "Lender Type"):
            if stop in raw:
                raw = raw[:raw.index(stop)].strip()
        if raw:
            at_list.append(raw)

    cg_re   = re.compile(r'Credit Grantor:\s*\n?\s*(.+?)(?:\n|$)', re.MULTILINE)
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


# ─────────────────────────────────────────────────────────────────
# ACCOUNT EXTRACTION
# ─────────────────────────────────────────────────────────────────

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
# MAIN CRIF PARSE  (called by parser.py orchestrator)
# ─────────────────────────────────────────────────────────────────

def parse_crif(text: str) -> tuple:
    """Returns (name, score, blocks, accounts, reported_totals)."""
    name     = extract_name(text)
    score    = extract_score(text)
    reported = extract_reported_totals(text)
    blocks   = split_account_blocks(text)

    at_list, cg_list = build_positional_lists(text)
    accounts = []
    for idx, (num, blk) in enumerate(blocks):
        loan_type = at_list[idx] if idx < len(at_list) else None
        entity    = cg_list[idx] if idx < len(cg_list) else None
        accounts.append(extract_account(num, blk, loan_type, entity))

    accounts.sort(key=lambda x: x["sr_no"])
    return name, score, blocks, accounts, reported
