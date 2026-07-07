"""
tu_parser.py  -  TransUnion CIBIL parser (commercial / company reports)
Rule-based extraction for Credit Facility blocks.
"""

import re


# ─────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────

def _to_int(s) -> int:
    try:
        return int(float(str(s).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0


def _parse_inr(s: str) -> int:
    if not s or s.strip() in ("-", ""):
        return 0
    s = re.sub(r"[₹\s,]", "", s.strip())
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


_MON = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


def _tu_date(s: str) -> str:
    """Convert '30-SEP-2025' → '30-09-2025'."""
    m = re.match(r"(\d{2})-([A-Z]{3})-(\d{4})", str(s).strip())
    if m:
        return f"{m.group(1)}-{_MON.get(m.group(2), m.group(2))}-{m.group(3)}"
    return s if s and s != "-" else "NA"


# ─────────────────────────────────────────────────────────────────
# NAME & SCORE
# ─────────────────────────────────────────────────────────────────

def extract_name(text: str) -> str:
    m = re.search(
        r"\bNAME\s*\n\s*(?:MS\s+)?([A-Z][A-Z0-9\s&.,'\-()]+?)(?:\n|BUSINESS)",
        text,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"SEARCH CRITERIA:\s*\n?([A-Z][A-Z0-9\s&.,'\-()]+?),\s*\d{2}-", text)
    return m.group(1).strip() if m else "Unknown"


def extract_score(text: str):
    m = re.search(r"(CMR-\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"CIBIL\s+Score[:\s]+(\d{3})", text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return "NA"


# ─────────────────────────────────────────────────────────────────
# VALIDATION TOTALS  (from Credit Summary)
# ─────────────────────────────────────────────────────────────────

def extract_totals(text: str) -> dict:
    """
    Extract Total CF count and Total outstanding from Credit Summary.
    Uses max values (the TOTAL row) to cover all institutions.
    """
    totals = {"account_count": None, "total_balance": None}
    m = re.search(r"CREDIT SUMMARY", text, re.IGNORECASE)
    if not m:
        return totals
    section = text[m.start(): m.start() + 3000]

    cf_counts = [int(x) for x in re.findall(r"Total CF.s\s*:\s*(\d+)", section)]
    if cf_counts:
        totals["account_count"] = max(cf_counts)

    # Some TU formats show a single total as ₹X(100%)
    bal_vals = [_to_int(x) for x in re.findall(r"₹([\d,]+)\(100%\)", section)]
    if bal_vals:
        totals["total_balance"] = max(bal_vals)
    else:
        # Other TU formats (3-column layout: YOUR INSTITUTION | TOTAL | OUTSIDE) split
        # the TOTAL outstanding into two ₹X(N%) amounts (non-delinquent + delinquent).
        # The TOTAL block starts with "TOTAL" on its own line followed by "Total Lenders".
        m2 = re.search(r"\bTOTAL\b\s*\nTotal Lenders\s*:\s*\d+\n", section)
        if m2:
            # Limit to the TOTAL block only — stop before the next "Total Lenders" line
            # (which starts the OUTSIDE column data in the 3-column layout).
            next_tl = section.find("Total Lenders", m2.end())
            end = next_tl if next_tl > m2.end() else m2.start() + 500
            total_block = section[m2.start(): end]
            pct_amounts = [_to_int(x) for x in re.findall(r"₹([\d,]+)\(\d+%\)", total_block)]
            if pct_amounts:
                totals["total_balance"] = sum(pct_amounts)

    return totals


# ─────────────────────────────────────────────────────────────────
# BLOCK SPLITTING
# ─────────────────────────────────────────────────────────────────

_PRE_RE = re.compile(
    r"LAST REPORTED DATE\s*\n\s*:\s+\d{2}-[A-Z]{3}-\d{4}", re.MULTILINE
)
_CF_RE = re.compile(r"Credit Facility\s+(\d+)\s*\n", re.MULTILINE)

# Section headers that mark the boundary between borrower and guarantor facilities.
# Numbered (e.g. "11.", "12.") and dash can be ASCII or en-dash (normalised at runtime).
_GUARANTOR_HDR = re.compile(
    r"\d+\.\s*CREDIT\s+FACILITY\s+DETAILS\s*[-–]\s*AS\s+GUARANTOR",
    re.IGNORECASE,
)


def split_blocks(text: str) -> list:
    """
    Split on 'LAST REPORTED DATE : {date}' preamble markers.
    Each block contains the amounts table + CF header + DPD history.
    Returns list of (cf_number: int, block_text: str, start_pos: int).
    """
    starts = [m.start() for m in _PRE_RE.finditer(text)]
    if not starts:
        return []

    blocks = []
    for i, start in enumerate(starts):
        end        = starts[i + 1] if i + 1 < len(starts) else len(text)
        block_text = text[start:end]
        cf_m       = _CF_RE.search(block_text)
        if not cf_m:
            continue
        blocks.append((int(cf_m.group(1)), block_text, start))

    return blocks


# ─────────────────────────────────────────────────────────────────
# FIELD EXTRACTION
# ─────────────────────────────────────────────────────────────────

def _extract_account(cf_num: int, block: str, ownership: str = "Borrower") -> dict:
    # Sanction amount + EMI (two-column header, values follow)
    m = re.search(
        r"SANCTIONED INR\s*\nINSTALLMENT AMOUNT\s*\n(₹\s*-?[\d,]+|-)\s*\n(₹\s*-?[\d,]+|-)?",
        block,
    )
    sanction_amt = _parse_inr(m.group(1)) if m else 0
    emi          = _parse_inr(m.group(2)) if (m and m.group(2)) else 0

    # Outstanding balance (can be negative  -  e.g. overpayment)
    m = re.search(
        r"OUTSTANDING BALANCE\s*\n(?:SUIT FILED\s*\n)?(₹\s*-?[\d,]+|-)", block
    )
    balance = _parse_inr(m.group(1)) if m else 0

    # Overdue
    m = re.search(
        r"OVERDUE\s*\n(?:WRITTEN OFF[:\s]*\n)?(₹\s*-?[\d,]+|-)", block
    )
    overdue = _parse_inr(m.group(1)) if m else 0

    # Sanction date (first date after SANCTIONED header in DATES section)
    m = re.search(
        r"SANCTIONED\s*\n(?:SUIT FILED\s*\n)?(?:LOAN EXPIRY[^\n]*\n)?"
        r"(?:WILFUL[^\n]*\n)?(?:LOAN RENEWAL\s*\n)?(\d{2}-[A-Z]{3}-\d{4}|-)",
        block,
    )
    if not m:
        m = re.search(r"AMOUNTS.*?DATES.*?(\d{2}-[A-Z]{3}-\d{4})", block, re.DOTALL)
    date_val = _tu_date(m.group(1)) if m else "NA"

    # Loan type (line(s) after 'Credit Facility N')
    m = re.search(r"Credit Facility\s+\d+\s*\n([^\n]+)(?:\n([^\n]+))?", block)
    if m:
        line1 = m.group(1).strip()
        line2 = (m.group(2) or "").strip()
        # Join second line only when first line has an unclosed parenthesis
        if line2 and line1.count("(") > line1.count(")"):
            loan_type = (line1 + " " + line2).title()
        else:
            loan_type = line1.title()
    else:
        loan_type = "Unknown"

    # Entity
    m = re.search(r"MEMBER\s*:\s*\n([^\n]+)", block)
    entity = m.group(1).strip() if m else "Not Disclosed"
    if entity.upper() in ("NOT DISCLOSED", "-", ""):
        entity = "Not Disclosed"

    # Max DPD
    dpd_vals = [int(x) for x in re.findall(r"(\d+)\s+DPD", block) if int(x) < 900]
    max_dpd  = max(dpd_vals) if dpd_vals else 0

    # Status
    status = _get_status(block)

    return {
        "sr_no":            cf_num,
        "date_of_sanction": date_val,
        "sanction_amount":  sanction_amt,
        "current_balance":  balance,
        "emi":              emi,
        "overdue":          overdue,
        "entity":           entity,
        "ownership":        ownership,
        "type_of_loan":     loan_type,
        "max_dpd":          max_dpd,
        "status":           status,
    }


def _get_status(block: str) -> str:
    cf_pos = re.search(r"Credit Facility\s+\d+\s*\n", block)
    if cf_pos:
        area = block[cf_pos.start(): cf_pos.start() + 400]
        if re.search(
            r"Closed By Payment|Settled|Written Off|Written-off|NPA|LOSS\b",
            area, re.IGNORECASE,
        ):
            return "Closed"
        if re.search(r"\bOpen\b", area, re.IGNORECASE):
            return "Active"
    return "Active"


# ─────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────

def validate(accounts: list, reported: dict) -> dict:
    """
    Count  : all extracted accounts vs Total CF's.
    Balance: sum of active balances vs total outstanding.
    """
    issues        = []
    active        = [a for a in accounts if a.get("status") == "Active"]
    total_count   = len(accounts)
    active_balance = sum(a.get("current_balance", 0) for a in active)

    expected_count   = reported.get("account_count")
    expected_balance = reported.get("total_balance")

    if expected_count is not None and total_count != expected_count:
        issues.append(
            f"Account count mismatch: extracted {total_count}, "
            f"report says {expected_count}"
        )
    if expected_balance and expected_balance > 0:
        tolerance = max(expected_balance * 0.05, 1000)
        if abs(active_balance - expected_balance) > tolerance:
            issues.append(
                f"Balance mismatch: extracted Rs.{active_balance:,}, "
                f"report says Rs.{expected_balance:,}"
            )

    return {
        "valid":             len(issues) == 0,
        "issues":            issues,
        "extracted_count":   total_count,
        "extracted_balance": active_balance,
        "expected_count":    expected_count,
        "expected_balance":  expected_balance,
    }


# ─────────────────────────────────────────────────────────────────
# MAIN TU PARSE  (called by parser.py orchestrator)
# ─────────────────────────────────────────────────────────────────

def parse_transunion(text: str) -> tuple:
    """Returns (name, score, accounts, reported_totals, validation)."""
    name     = extract_name(text)
    score    = extract_score(text)
    reported = extract_totals(text)
    blocks   = split_blocks(text)

    # Find where the "AS GUARANTOR" section starts so every block before it
    # is tagged Borrower and every block at/after it is tagged Guarantor.
    guar_m      = _GUARANTOR_HDR.search(text)
    guarantor_pos = guar_m.start() if guar_m else None

    accounts = []
    for num, blk, start_pos in blocks:
        if guarantor_pos is not None and start_pos >= guarantor_pos:
            ownership = "Guarantor"
        else:
            ownership = "Borrower"
        accounts.append(_extract_account(num, blk, ownership))

    accounts.sort(key=lambda x: x["sr_no"])
    return name, score, accounts, reported, validate(accounts, reported)
