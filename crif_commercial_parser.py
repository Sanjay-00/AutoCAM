"""
crif_commercial_parser.py  -  CRIF High Mark "COMMERCIAL ACE REPORT" parser.

A third provider format (distinct from CRIF Retail/MSME and TransUnion). Unlike the
retail report, fields sit in a 3-column grid, so a single text line typically carries
two or three `Label: value` pairs:

    Account #: XXX   Amount Overdue: 0   Sanctioned Amount: 28,50,000
    Closure Reason: WRITTEN OFF   Closed Date:   Drawing Power: 0

Extraction is therefore inline (`Label:\\s*value`), NOT next-line like crif_parser.

The SAME parser consumes both:
  • digital CRIF Commercial text (page.get_text), and
  • OCR text from a scanned report (Tesseract),
so every pattern is written to tolerate OCR noise (stray glyphs, O/0 & l/1 slips,
collapsed whitespace).

Public API mirrors crif_parser.parse_crif:
    parse_crif_commercial(text) -> (name, score, blocks, accounts, reported)
"""

import re

# Reuse the shared int helper shape from crif_parser.
from crif_parser import to_int


# ─────────────────────────────────────────────────────────────────
# NAME & SCORE
# ─────────────────────────────────────────────────────────────────

def extract_name(text: str) -> str:
    """Borrower name from the Inquiry Details 'Name:' field (company row).

    Handles both inline ('Name: KB CABS  Legal Constitution:') and next-line
    ('Name:\\nKB CABS\\nLegal Constitution:') formats.
    """
    # Try inline first, then next-line
    for pat in (
        r'\bName:\s*([A-Z][A-Z0-9 &.,\'\-()]+?)\s+(?:Short\s+Name|Legal\s+Constitution)\b',
        r'\bName:\s*\n\s*([A-Z][A-Z0-9 &.,\'\-()]+?)\s*\n\s*(?:Short\s+Name|Legal\s+Constitution)\b',
    ):
        m = re.search(pat, text)
        if m:
            return re.sub(r'\s{2,}', ' ', m.group(1)).strip()
    m = re.search(r'\bName:\s*\n?\s*([A-Z][A-Z0-9 &.,\'\-()]{2,})', text)
    return re.sub(r'\s{2,}', ' ', m.group(1)).strip() if m else "Unknown"


# Commercial CRIF risk grade → rank (1 best … 5 worst). From the report legend:
#   A-D,1: Very Low; E-G,2: Low; H-I,3: Medium; J-K,4: High; L-M,5: Very High.
_RISK_RANK = {
    "VERY LOW RISK": 1, "LOW RISK": 2, "MEDIUM RISK": 3,
    "HIGH RISK": 4, "VERY HIGH RISK": 5,
}


def extract_score(text: str):
    """
    CRIF Commercial isn't a 300-900 score  -  it's a PERFORM COMMERCIAL risk RANK
    from 1 (best) to 5 (worst), printed as a grade letter + label, e.g.
    'PERFORM COMMERCIAL 2.0 ... J-High Risk'. We key on the risk LABEL (robust)
    rather than the OCR-fragile single grade letter, and return 'N (Label)'.
    """
    m = re.search(r'PERFORM\s+COMMERCIAL', text, re.IGNORECASE)
    if m:
        rest = text[m.start():]
        tip  = re.search(r'\bTip\b', rest)               # exclude the legend line
        region = rest[: tip.start() if tip else 200]
        rm = re.search(r'((?:Very\s+)?(?:Low|Medium|High)\s+Risk)', region, re.IGNORECASE)
        if rm:
            label = re.sub(r'\s+', ' ', rm.group(1)).strip().title()
            rank  = _RISK_RANK.get(label.upper())
            return f"{rank} ({label})" if rank else label
    # Fall back to the older 'ranked as <X> Risk' phrasing if present.
    m = re.search(r'ranked\s+as\s+((?:Very\s+)?(?:Low|Medium|High)\s+Risk)', text, re.IGNORECASE)
    if m:
        label = re.sub(r'\s+', ' ', m.group(1)).strip().title()
        rank  = _RISK_RANK.get(label.upper())
        return f"{rank} ({label})" if rank else label
    return "NA"


# ─────────────────────────────────────────────────────────────────
# VALIDATION TOTALS  (Borrower Summary  -  amounts in Crores)
# ─────────────────────────────────────────────────────────────────

# Borrower Summary row columns (after the institution label line):
#   Lender(#) Total Accts(#) Live Accts(#) Delinquent(#) Sanctioned(x%) Outstanding Overdue PAR(90+)
# Amounts are in CRORES; per-account figures are in rupees, so convert on the way out.
_CRORE = 1_00_00_000


def _summary_section(text: str) -> str:
    m = re.search(r'Borrower\s+Summary', text, re.IGNORECASE)
    if not m:
        return ""
    end = re.search(r'Credit\s+Profile\s+Summary', text[m.end():], re.IGNORECASE)
    stop = m.end() + (end.start() if end else 2500)
    return text[m.start():stop]


def _parse_summary_row(row: str, live_idx: int = 2):
    """
    Return (live_accts, outstanding_crore) from one institution data row, or None.

    Expected active = the 'Live Accts' column only. The bureau reports Delinquent
    accounts in a separate column; our extraction counts a delinquent (open,
    balance>0) facility as Active, so it can legitimately exceed the bureau's Live
    count. We deliberately do NOT add Delinquent here  -  that mismatch should be
    surfaced to the analyst, not silently absorbed.

    Two row shapes exist across CRIF Commercial report versions:
      Format A (Lender# inline): '19 237 116 0 29.36 (100%) 19.9 ...'
        → ints[Lender#, Total, Live, Delinq, ...]  live_idx=2
      Format B (Lender# on its own line): '2 1 1 0.23 (38.98%) 0.01 ...'
        → ints[Total, Live, Delinq, ...]            live_idx=1
    """
    ints = re.findall(r'(?<![\d.])\d{1,5}(?![\d.])', row)
    live = to_int(ints[live_idx]) if len(ints) > live_idx else None
    # Outstanding = first decimal AFTER the '(x%)' token. The percentage itself
    # may be decimal, e.g. '(43.43%)', so allow a dot inside it.
    m = re.search(r'\(\s*[\d.]+\s*%\s*\)\s*([\d]+\.?\d*)', row)
    outstanding = float(m.group(1)) if m else None
    if live is None and outstanding is None:
        return None
    return live, outstanding


def extract_reported_totals(text: str) -> dict:
    """
    Sum Live Accts and Outstanding across the 'Your Institution' and 'Other
    Institution' rows of the Borrower Summary. Outstanding (Crores) is converted to
    rupees to match per-account current_balance units.
    """
    totals = {"account_count": None, "total_balance": None}
    section = _summary_section(text)
    if not section:
        return totals

    # Detect row format: if Lender(#) appears on the same header line as Total Accts,
    # data rows include Lender# as the first column (live_idx=2); otherwise live_idx=1.
    lender_inline = bool(re.search(r'Lender.{0,30}Total\s+Accts', section, re.IGNORECASE))
    live_idx = 2 if lender_inline else 1

    active_sum, out_sum, found = 0, 0.0, False
    for label in ("Your Institution", "Other Institution"):
        m = re.search(re.escape(label) + r'\s*\n([^\n]+)', section)
        if not m:
            continue
        parsed = _parse_summary_row(m.group(1), live_idx=live_idx)
        if not parsed:
            continue
        active, outstanding = parsed
        if active is not None:
            active_sum += active
            found = True
        if outstanding is not None:
            out_sum += outstanding
            found = True

    if found:
        totals["account_count"] = active_sum
        totals["total_balance"] = int(round(out_sum * _CRORE)) if out_sum else None
    return totals


# ─────────────────────────────────────────────────────────────────
# ACCOUNT BLOCK SPLITTING
# ─────────────────────────────────────────────────────────────────

# Each account header reads: "Loan Terms For: Applicant as Borrower  Info. as of: <date>".
# OCR sometimes garbles "Terms For" but leaves "Info. as of" intact (or vice-versa),
# so we anchor on EITHER token. Require the trailing colon so we don't match prose.
_BLOCK_MARKER = re.compile(r'(?:Loan\s+)?Terms\s+For\s*:', re.IGNORECASE)
_INFO_MARKER  = re.compile(r'Info\.?\s*as\s*of\s*:',       re.IGNORECASE)
_HEADER_DEDUP = 120   # Terms-For and Info-as-of co-occur within ~40 chars on one line


def split_account_blocks(text: str) -> list:
    """
    Each detailed account ('Account Trade History') begins at its header line.
    Anchor on 'Loan Terms For' OR 'Info. as of' (whichever OCR preserved) and
    de-duplicate the two tokens that share a single header line. This recovers
    accounts whose 'Terms For' header was mangled by OCR (e.g. on long reports).
    Returns list of (ordinal: int, block_text: str).
    """
    cands = sorted([m.start() for m in _BLOCK_MARKER.finditer(text)]
                   + [m.start() for m in _INFO_MARKER.finditer(text)])
    if not cands:
        return []
    starts = [cands[0]]
    for p in cands[1:]:
        if p - starts[-1] > _HEADER_DEDUP:
            starts.append(p)
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append((i + 1, text[start:end]))
    return blocks


# ─────────────────────────────────────────────────────────────────
# FIELD EXTRACTION  (inline 'Label: value', value stops at next known label)
# ─────────────────────────────────────────────────────────────────

# Labels that terminate a free-text value (so we don't swallow the next column).
# Kept loose (e.g. 'sset Classification') because OCR mangles 'DPD/Asset'.
_STOP = (
    r'Type:|Lender:|Account\s*#:|Amount\s+Overdue:|Sanctioned\s+(?:Date|Amount):|'
    r'Current\s+Balance:|Closure\s+(?:Reason|Status):|Closed\s+Date:|Drawing\s+Power:|'
    r'DPD\s*/\s*Asset|sset\s+Classification|Classification:|'
    r'Last\s+Payment\s+Date:|Written\s+Off\s+Amt:|Info\.?\s+as\s+of'
)


def _field(block: str, label: str) -> str:
    """Inline value after `label`, trimmed at the next column label or newline."""
    m = re.search(label + r'\s*[:.]?\s*(.*?)(?:' + _STOP + r'|\n|$)', block, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _amount(block: str, label: str) -> int:
    m = re.search(label + r'\s*[:.]?\s*(-?[\d,]+)', block, re.IGNORECASE)
    return to_int(m.group(1)) if m else 0


def _extract_date(block: str) -> str:
    m = re.search(r'Sanctioned\s+Date\s*[:.]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE)
    return m.group(1) if m else "NA"


def _extract_ownership(block: str) -> str:
    """
    Ownership comes from 'Loan Terms For: <role>  Info. as of: <date>'.
    'Applicant as Borrower' → 'Borrower'; 'Guarantor' → 'Guarantor'; etc.
    """
    m = re.search(
        r'(?:Loan\s+)?Terms\s+For\s*:\s*(.*?)(?:Info\.?\s*as\s*of|$)',
        block, re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    val = re.sub(r'\s+', ' ', m.group(1)).strip()
    if re.search(r'borrower', val, re.IGNORECASE):
        return "Borrower"
    if re.search(r'guarantor', val, re.IGNORECASE):
        return "Guarantor"
    return val.title() if val else ""


def _extract_entity(block: str) -> str:
    val = _field(block, r'Lender')
    val = re.sub(r'[^\w &.,\'\-()/]', '', val).strip()
    if not val or re.match(r'^[X0OK]{2,}$', val, re.IGNORECASE):
        return "NA"
    return val


_LOAN_TYPE_NORMALIZE = {
    r'long\s+term\s+loan\s*\(period\s+above\b.*':                    'Long term loan (>3 years)',
    r'short\s+term\s+loan\s*\(period\s+up\b.*':                       'Short term loan (<3 years)',
    r'short\s+term\s+loan\s*\(less\s+than\s+1\b.*':                  'Short term loan (<1 year)',
    r'above\s+1\s+year\s+and\s+upto\s+3\s+years\b.*':               'Medium term loan (1-3 years)',
    r'^-?\s*in\s+inr\s*$':                                            'Unknown',
    r'^in\s+inr\s*$':                                                  'Unknown',
    r'.*\bsecured\b.*\bbusiness\s+loan\b.*':                           'Unsecured Business Loan -In INR',
    r'.*\bbusiness\s+loan\b.*':                                        'Business Loan -In INR',
}


def _extract_loan_type(block: str) -> str:
    val = _field(block, r'Type')
    val = re.sub(r'\s{2,}', ' ', val).strip(' -')
    # Strip OCR garbage from the DPD/Asset Classification column that bleeds in
    # when the line has no clean separator: "Long term loan (period above np /psset..."
    val = re.sub(r'\s+(?:np\s*/|[A-Z]{2,}/|\d+\s*/|DPD)\s*.*$', '', val, flags=re.IGNORECASE).strip()
    # Stop at any ALL-CAPS word sequence that looks like a new field heading
    m = re.search(r'\s{2,}[A-Z]{2,}', val)
    if m:
        val = val[:m.start()].strip()
    # Normalize known truncated / noisy OCR loan type strings
    for pat, replacement in _LOAN_TYPE_NORMALIZE.items():
        if re.match(pat, val, re.IGNORECASE):
            return replacement
    # Fix common OCR character substitutions
    val = re.sub(r'Wenicle|Welnicle|Venicle', 'Vehicle', val, flags=re.IGNORECASE)
    val = re.sub(r"[=\'`]+(\w)", r' \1', val).strip()   # apostrophe mid-word → space
    val = re.sub(r'[=\'`]+$', '', val).strip()
    # Title-case, then restore known all-caps tokens
    val = val.title() if val else "Unknown"
    val = re.sub(r'\bInr\b', 'INR', val)
    return val


def _extract_max_dpd(block: str) -> int:
    """
    DPD from the Payment History grid ('NNN/SMA', 'NNN/xxx', ...).

    CRIF Commercial uses 'xxx' as the class code placeholder (not 'STD').
    A non-zero NNN paired with 'xxx' is therefore a genuine DPD reading.

    OCR routinely misreads the leading zeros of '000/xxx' as '200/xxx', which
    would fabricate delinquency. We guard against this by accepting 'xxx' cells
    only when DPD >= 10 (single-digit OCR drift from 000 is implausible at that
    magnitude). Named non-standard buckets (SMA/SUB/DBT/LOS) are trusted at any
    positive value. Vision fallback recovers precise DPD when OCR fails on
    colored cells.
    """
    vals = []
    # Named non-standard buckets: any positive value is real delinquency
    for m in re.finditer(r'\b(\d{1,3})\s*/\s*(?:SMA|SUB|DBT|LOS)\b', block, re.IGNORECASE):
        v = int(m.group(1))
        if 0 < v < 999:
            vals.append(v)
    # CRIF Commercial 'xxx' class: accept when >= 10 to filter OCR noise on 000
    for m in re.finditer(r'\b(\d{1,3})\s*/\s*xxx\b', block, re.IGNORECASE):
        v = int(m.group(1))
        if 10 <= v < 999:
            vals.append(v)
    return max(vals) if vals else 0


# Non-standard asset classes (a positive DPD here is genuine delinquency).
_NONSTD_DPD = re.compile(
    r'(?<!\d)(\d{1,3})\s*/\s*(?:SMA|SMO|SM\d|SUB|DBT|DB\d|LOS|NPA|ARC|xxx)\b', re.IGNORECASE)


def nonstandard_dpd_by_date(text: str) -> dict:
    """
    Map {sanctioned_date: dpd} from the 'Top 5 Non-Standard Facilities' summary
    table. That table prints each delinquent facility's DPD cleanly (e.g. '57/SMA')
    right beside its Sanctioned Date  -  far more reliable than the tiny, OCR-mangled
    per-account payment grid. We join it back to accounts on Sanctioned Date.
    """
    m = re.search(r'Top\s*\d?\s*Non[\s-]*Standard\s+Facilities', text, re.IGNORECASE)
    if not m:
        return {}
    rest = text[m.end():]
    stop = re.search(r'Firmographic|Index\s+of\s+Charges|Company.?s\s+Key|Relationship',
                     rest, re.IGNORECASE)
    section = rest[: stop.start() if stop else 1500]

    dpd_tokens = [(mm.start(), int(mm.group(1))) for mm in _NONSTD_DPD.finditer(section)]
    if not dpd_tokens:
        return {}
    result = {}
    for dm in re.finditer(r'\d{2}-\d{2}-\d{4}', section):
        near = min(dpd_tokens, key=lambda t: abs(t[0] - dm.start()))
        if abs(near[0] - dm.start()) < 120:          # date and DPD in the same row
            d = dm.group(0)
            result[d] = max(result.get(d, 0), near[1])
    return result


_CLOSED_KEYWORDS = re.compile(
    r'WRITTEN\s*-?\s*OFF|SETTLED|\bCLOSED\b|RESTRUCTUR|\bSOLD\b|POST\s*\(?\s*WO',
    re.IGNORECASE,
)


def _is_closed(block: str, current_balance: int = None) -> bool:
    # Rule 0 (primary): the bureau's own colour-coded status strip, read from the
    # left margin during OCR and injected as a marker. Most reliable signal  - 
    # the per-account Closure Reason/Closed Date fields are usually blank.
    if "__STATUS_CLOSED__" in block:
        return True
    if "__STATUS_ACTIVE__" in block:
        return False
    # Rule 1: the Closure Reason VALUE names a terminal status.
    reason = _field(block, r'Closure\s+(?:Reason|Status)')
    if reason and _CLOSED_KEYWORDS.search(reason):
        return True
    # Rule 2: a real Closed Date is present.
    if re.search(r'Closed\s+Date\s*[:.]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE):
        return True
    # Rule 3: a non-zero written-off amount.
    wo = re.search(r'Written\s+Off\s+Amt\s*[:.]?\s*([\d,]+)', block, re.IGNORECASE)
    if wo and to_int(wo.group(1)) > 0:
        return True
    # Rule 4 (fallback, no strip / no explicit signal): the account is LIVE if it
    # still has money against it  -  outstanding balance > 0 OR an open drawing power
    # (a sanctioned-but-undrawn facility is still live). Only when both are zero do
    # we treat it as closed. Current balance is the figure that actually drives the
    # report total, so a misclassified zero-balance account costs nothing there.
    drawing_power = _amount(block, r'Drawing\s+Power')
    if (current_balance and current_balance > 0) or drawing_power > 0:
        return False
    # Exception: reported on its very first day (Info. as of == Sanctioned Date)  - 
    # newly originated, zero balance just means not yet drawn. Keep it live.
    if current_balance is not None and current_balance == 0:
        sanction_date = re.search(r'Sanctioned\s+Date\s*[:.]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE)
        info_as_of    = re.search(r'Info\.?\s*as\s*of\s*[:]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE)
        if sanction_date and info_as_of and sanction_date.group(1) == info_as_of.group(1):
            return False  # brand-new account, not yet drawn
    return True


def extract_account(ordinal: int, block: str) -> dict:
    balance = _amount(block, r'Current\s+Balance')
    # Status uses the raw block (it carries the injected strip marker); all other
    # fields use a cleaned copy so the marker can't bleed into entity/loan type.
    status = "Closed" if _is_closed(block, balance) else "Active"
    clean  = re.sub(r'__STATUS_(?:ACTIVE|CLOSED)__', '', block)
    return {
        "sr_no":            ordinal,
        "date_of_sanction": _extract_date(clean),
        "sanction_amount":  _amount(clean, r'Sanctioned\s+Amount'),
        "current_balance":  balance,
        "emi":              _amount(clean, r'Installment\s+Amount|EMI'),
        "overdue":          _amount(clean, r'Amount\s+Overdue'),
        "entity":           _extract_entity(clean),
        "ownership":        _extract_ownership(clean),
        "type_of_loan":     _extract_loan_type(clean),
        "max_dpd":          _extract_max_dpd(clean),
        "status":           status,
    }


# ─────────────────────────────────────────────────────────────────
# MAIN PARSE  (called by parser.py orchestrator)
# ─────────────────────────────────────────────────────────────────

def parse_crif_commercial(text: str) -> tuple:
    """Returns (name, score, blocks, accounts, reported_totals)."""
    name     = extract_name(text)
    score    = extract_score(text)
    reported = extract_reported_totals(text)
    blocks   = split_account_blocks(text)
    accounts = [extract_account(num, blk) for num, blk in blocks]

    # Override DPD from the authoritative 'Top 5 Non-Standard Facilities' table  - 
    # it reports each delinquent facility's DPD cleanly; the per-account payment
    # grid OCRs too poorly to trust. Joined back to accounts on Sanctioned Date.
    topn = nonstandard_dpd_by_date(text)
    if topn:
        for a in accounts:
            d = a.get("date_of_sanction")
            if d in topn:
                a["max_dpd"] = max(a["max_dpd"], topn[d])

    accounts.sort(key=lambda x: x["sr_no"])
    return name, score, blocks, accounts, reported
