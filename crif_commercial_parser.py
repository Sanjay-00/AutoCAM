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
    # Collapse whitespace first: some digital PDFs put each header/cell on its own
    # text line (one value per line) rather than joining a row onto one line the
    # way OCR'd/scanned text does, which would otherwise break both this check and
    # the row capture below.
    flat = re.sub(r'\s+', ' ', section)
    lender_inline = bool(re.search(r'Lender.{0,30}Total\s+Accts', flat, re.IGNORECASE))
    live_idx = 2 if lender_inline else 1

    active_sum, out_sum, found = 0, 0.0, False
    for label in ("Your Institution", "Other Institution"):
        m = re.search(
            re.escape(label) + r'\s*\n(.*?)(?=\n(?:Your Institution|Other Institution)\b|\n\*\s*Only|\Z)',
            section, re.DOTALL,
        )
        if not m:
            continue
        row = re.sub(r'\s+', ' ', m.group(1)).strip()
        parsed = _parse_summary_row(row, live_idx=live_idx)
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


def _group_starts(text: str) -> list:
    """Start positions of each 'Loan Terms For' group (see split_account_blocks)."""
    cands = sorted([m.start() for m in _BLOCK_MARKER.finditer(text)]
                   + [m.start() for m in _INFO_MARKER.finditer(text)])
    if not cands:
        return []
    starts = [cands[0]]
    for p in cands[1:]:
        if p - starts[-1] > _HEADER_DEDUP:
            starts.append(p)
    return starts


def split_account_blocks(text: str) -> list:
    """
    Each detailed account ('Account Trade History') begins at its header line.
    Anchor on 'Loan Terms For' OR 'Info. as of' (whichever OCR preserved) and
    de-duplicate the two tokens that share a single header line. This recovers
    accounts whose 'Terms For' header was mangled by OCR (e.g. on long reports).
    Returns list of (ordinal: int, block_text: str).
    """
    starts = _group_starts(text)
    if not starts:
        return []
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append((i + 1, text[start:end]))
    return blocks


# Alternate per-account anchor. Some CRIF Commercial digital layouts group
# several 'Account/ Trade History' entries (each with its own Sanctioned Date,
# Sanctioned Amount, Current Balance, Lender) under a single 'Loan Terms For:'
# balance-history block, while other 'Loan Terms For:' blocks end up with none
# of the entries that belong to them  -  split_account_blocks then misattributes
# accounts (e.g. 2 trade entries in block N, 0 in block N+1) or drops real
# accounts as "phantom". Each trade entry's own 'Type:' field line precedes its
# financial fields and does not repeat elsewhere, so it anchors 1:1 with the
# report's true account count in that layout.
_TRADE_MARKER = re.compile(r'\bType:\s*\n')


def split_trade_blocks(text: str) -> list:
    """Returns list of (ordinal, block_text), one per 'Type:' occurrence."""
    starts = [m.start() for m in _TRADE_MARKER.finditer(text)]
    if not starts:
        return []
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        blocks.append((i + 1, text[start:end]))
    return blocks


def _ownership_before(text: str, pos: int) -> str:
    """
    Trade blocks (split_trade_blocks) don't reliably carry their own 'Loan Terms
    For:' role field, since that label sits in a different section than the
    'Type:'-anchored financial fields. Look backward for the nearest one instead
    - the role applies to every trade entry until the next 'Loan Terms For:'.
    """
    role = ""
    for m in _BLOCK_MARKER.finditer(text, 0, pos):
        role = m
    if not role:
        return ""
    tail = text[role.end():role.end() + 200]
    val  = re.sub(r'\s+', ' ', _INFO_MARKER.split(tail)[0]).strip()
    if re.search(r'borrower', val, re.IGNORECASE):
        return "Borrower"
    if re.search(r'guarantor', val, re.IGNORECASE):
        return "Guarantor"
    if re.search(r'co-?applicant|co-?borrower', val, re.IGNORECASE):
        return "Co-Applicant"
    return ""


def _quality(accounts: list, reported: dict) -> float:
    """Combined relative error of extracted active count/balance vs reported totals. Lower is better."""
    active = [a for a in accounts if a.get("status") == "Active"]
    ec, eb = len(active), sum(a.get("current_balance") or 0 for a in active)
    xc, xb = reported.get("account_count"), reported.get("total_balance")
    err = 0.0
    if xc:
        err += abs(ec - xc) / xc
    if xb:
        err += abs(eb - xb) / xb
    return err


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
    r'.*\bsecured\b.*\bbusiness\s+loan\b.*':                           'Unsecured Business Loan',
    r'.*\bbusiness\s+loan\b.*':                                        'Business Loan',
    r'[cf]ommercial\s*vehicle\s*loan\b.*':                            'Commercial Vehicle Loan',
    r'construction\s*equipment\s*loan\b.*':                           'Construction Equipment Loan',
    r'equipment\s+financ.*':                                           'Equipment Financing',
    r'^\(construction.*office.*medical.*\).*':                        'Equipment Financing',
    r'^\(.*\)$':                                                       'Equipment Financing',
}


def _extract_loan_type(block: str) -> str:
    # Require a literal colon after 'Type' (the real trade label is always 'Type:').
    # A loose substring match on 'Type' also hits the unrelated 'Type of Relationship'
    # applicant-details header that can precede the real label in the same block.
    m = re.search(r'\bType\s*:\s*(.*?)(?:' + _STOP + r'|\n|$)', block, re.IGNORECASE)
    val = m.group(1).strip() if m else ""
    val = re.sub(r'\s{2,}', ' ', val).strip(' -')
    # Strip "- In INR" / "-In INR" currency suffix (appears on same line as loan type)
    val = re.sub(r'\s*-?\s*In\s+INR\s*$', '', val, flags=re.IGNORECASE).strip(' -')
    # Strip OCR garbage from the DPD/Asset Classification column that bleeds in
    val = re.sub(r'\s+(?:np\s*/|[A-Z]{2,}/|\d+\s*/|DPD)\s*.*$', '', val, flags=re.IGNORECASE).strip()
    # Stop at any ALL-CAPS word sequence that looks like a new field heading
    m = re.search(r'\s{2,}[A-Z]{2,}', val)
    if m:
        val = val[:m.start()].strip()
    # Normalize known truncated / noisy OCR loan type strings
    for pat, replacement in _LOAN_TYPE_NORMALIZE.items():
        if re.match(pat, val, re.IGNORECASE):
            return replacement
    # Fallback: if what we extracted is clearly OCR noise (too short or a known garbage
    # token like "ppp"), search the full block text for a recognizable loan type phrase.
    # This recovers cases where column-bleed puts the type text before the "Type:" label.
    if len(val) <= 4 or val.lower() in ('ppp', 'std', 'sma', 'sub', 'dbt', 'los'):
        for pat, replacement in _LOAN_TYPE_NORMALIZE.items():
            if re.search(pat, block, re.IGNORECASE):
                return replacement
        return "Unknown"
    # Fix common OCR character substitutions
    val = re.sub(r'Wenicle|Welnicle|Venicle', 'Vehicle', val, flags=re.IGNORECASE)
    val = re.sub(r"[=\'`]+(\w)", r' \1', val).strip()   # apostrophe mid-word → space
    val = re.sub(r'[=\'`]+$', '', val).strip()
    # Title-case, then restore known all-caps tokens
    val = val.title() if val else "Unknown"
    val = re.sub(r'\bInr\b', 'INR', val)
    return val


def _extract_max_dpd(block: str) -> int | None:
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

    Returns None (not 0) when the grid pattern wasn't found at all in the
    block - that means OCR couldn't read the payment-history table, which is
    a different situation from confidently reading it as all-zero/standard.
    Callers render None as "Check CIBIL" instead of a silent 0.
    """
    raw_matches = list(re.finditer(r'\b\d{1,3}\s*/\s*(?:SMA|SUB|DBT|LOS|xxx)\b', block, re.IGNORECASE))
    if not raw_matches:
        return None

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


_HTML_STATUS_BADGE = re.compile(
    r'Info\.?\s*as\s*\n?\s*of\s*:\s*\d{2}-\d{2}-\d{4}\s*\n?\s*(ACTIVE|CLOSED)\b'
)

# Some digital-HTML layouts don't render the status badge adjacent to 'Info.
# as of:' at all (see _HTML_STATUS_BADGE) - instead every trade under one
# 'Loan Terms For' group gets a bare 'ACTIVE'/'CLOSED' line, but ALL of that
# group's badges are rendered together at the END of the group's span (after
# its last trade's fields), not one immediately after each trade. Within a
# group, badge count always matches trade count and both are in the same
# document order, so they can be zipped 1:1 per group even though neither
# split_account_blocks nor split_trade_blocks separates the trades any other
# way.
_STATUS_BADGE_LINE = re.compile(r'^\s*(CLOSED|ACTIVE)\s*$', re.MULTILINE)


def _positional_trade_status(text: str, trade_starts: list) -> dict:
    """
    Maps trade index (0-based, matching `trade_starts` order) -> 'Active' /
    'Closed' using per-group badge positions (see module note above). A group
    whose trade count doesn't match its badge count (including zero badges -
    layouts using the adjacent-badge format instead) contributes nothing;
    callers keep using _is_closed()'s rule-based fallback for those trades.
    """
    if not trade_starts:
        return {}
    badges = [(m.start(), m.group(1).title()) for m in _STATUS_BADGE_LINE.finditer(text)]
    if not badges:
        return {}

    group_starts = _group_starts(text)
    if not group_starts:
        return {}

    result = {}
    for g, gstart in enumerate(group_starts):
        gend = group_starts[g + 1] if g + 1 < len(group_starts) else len(text)
        trades_here = [i for i, t in enumerate(trade_starts) if gstart <= t < gend]
        badges_here = [s for p, s in badges if gstart <= p < gend]
        if trades_here and len(trades_here) == len(badges_here):
            for ti, status in zip(trades_here, badges_here):
                result[ti] = status
    return result


def _is_closed(block: str, current_balance: int = None) -> bool:
    # Rule 0 (primary): the bureau's own colour-coded status strip. For scanned
    # PDFs it's read from the left margin during OCR and injected as a marker;
    # for HTML sources the same badge is literal text right after 'Info. as
    # of: <date>' (a colour-coded pill in the rendered page). Most reliable
    # signal either way  -  the per-account Closure Reason/Closed Date fields
    # are usually blank even on accounts that are genuinely closed.
    if "__STATUS_CLOSED__" in block:
        return True
    if "__STATUS_ACTIVE__" in block:
        return False
    m = _HTML_STATUS_BADGE.search(block)
    if m:
        return m.group(1).upper() == "CLOSED"
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
    # Fallback: scanned OCR sometimes can't read the Current Balance field but
    # CAN read Drawing Power (same value for active facilities). Use it when
    # balance is 0 and drawing power is present.
    if balance == 0:
        dp = _amount(block, r'Drawing\s+Power')
        if dp > 0:
            balance = dp
    # Status uses the raw block (it carries the injected strip marker); all other
    # fields use a cleaned copy so the marker can't bleed into entity/loan type.
    status = "Closed" if _is_closed(block, balance) else "Active"
    clean  = re.sub(r'__STATUS_(?:ACTIVE|CLOSED)__', '', block)
    sanction = _amount(clean, r'Sanctioned\s+Amount')
    # Use None for amounts that are 0 on Active accounts — signals a read failure
    # (0 is never valid for sanction/balance on a live loan). Displayed as
    # "Check CIBIL" in the app and Excel.
    if status == "Active":
        if sanction == 0:
            sanction = None
        if balance == 0:
            balance = None
    return {
        "sr_no":            ordinal,
        "date_of_sanction": _extract_date(clean),
        "sanction_amount":  sanction,
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

def _is_phantom(a: dict) -> bool:
    """Digital PDFs produce orphan fragments (balance-history columns split off
    by a repeated 'Loan Terms For' page header) with no extractable date,
    sanction amount, or balance — not real accounts."""
    return a["date_of_sanction"] == "NA" and a["sanction_amount"] == 0 and a["current_balance"] == 0


def _expand_account_blocks(text: str, trade_starts: list, status_map: dict) -> tuple:
    """
    Per-group account extraction that fixes the case where a page break
    rendered 2+ trades' data inside a single 'Loan Terms For' group (see
    _positional_trade_status) — extract_account()'s regex fields only match
    the FIRST occurrence in a block, so a naive whole-group extraction
    silently drops every trade after the first. Groups with exactly one
    trade (the common case) are extracted from the whole group span exactly
    as before. Groups with more than one trade are expanded into one dict
    per trade, each sliced from its own 'Type:' marker to the next one
    anywhere in the document (matching split_trade_blocks), with ownership
    backfilled from the shared group header and status resolved from
    status_map when available.
    Returns (blocks, accounts) - blocks as (ordinal, block_text) pairs.
    """
    group_starts = _group_starts(text)
    blocks, accounts, ordinal = [], [], 0
    for g, gstart in enumerate(group_starts):
        gend = group_starts[g + 1] if g + 1 < len(group_starts) else len(text)
        trades_here = [i for i, t in enumerate(trade_starts) if gstart <= t < gend]
        if len(trades_here) <= 1:
            ordinal += 1
            blk = text[gstart:gend]
            a = extract_account(ordinal, blk)
            if trades_here and trades_here[0] in status_map:
                a["status"] = status_map[trades_here[0]]
            blocks.append((ordinal, blk))
            accounts.append(a)
            continue
        for ti in trades_here:
            ordinal += 1
            t_start = trade_starts[ti]
            t_end   = trade_starts[ti + 1] if ti + 1 < len(trade_starts) else len(text)
            blk = text[t_start:t_end]
            a = extract_account(ordinal, blk)
            if not a["ownership"]:
                a["ownership"] = _ownership_before(text, t_start)
            if ti in status_map:
                a["status"] = status_map[ti]
            blocks.append((ordinal, blk))
            accounts.append(a)
    return blocks, accounts


def parse_crif_commercial(text: str) -> tuple:
    """Returns (name, score, blocks, accounts, reported_totals)."""
    name     = extract_name(text)
    score    = extract_score(text)
    reported = extract_reported_totals(text)

    trade_starts = [m.start() for m in _TRADE_MARKER.finditer(text)]
    status_map   = _positional_trade_status(text, trade_starts)

    blocks, accounts = _expand_account_blocks(text, trade_starts, status_map)
    accounts = [a for a in accounts if not _is_phantom(a)]

    # 'Loan Terms For:' blocks can still misattribute accounts even after the
    # merged-group expansion above (e.g. a layout where account-format's own
    # grouping doesn't line up with trades at all) — always compare against
    # the pure 'Type:'-anchored trade split and keep whichever is closer to
    # the report's own totals, the same way parser.py picks between OCR and
    # Vision extraction.
    trade_blocks = split_trade_blocks(text)
    if trade_blocks:
        trade_accounts = []
        for i, ((num, blk), start) in enumerate(zip(trade_blocks, trade_starts)):
            a = extract_account(num, blk)
            if not a["ownership"]:
                a["ownership"] = _ownership_before(text, start)
            if i in status_map:
                a["status"] = status_map[i]
            trade_accounts.append(a)
        trade_accounts = [a for a in trade_accounts if not _is_phantom(a)]
        if trade_accounts and _quality(trade_accounts, reported) < _quality(accounts, reported):
            blocks, accounts = trade_blocks, trade_accounts

    # Override DPD from the authoritative 'Top 5 Non-Standard Facilities' table  -
    # it reports each delinquent facility's DPD cleanly; the per-account payment
    # grid OCRs too poorly to trust. Joined back to accounts on Sanctioned Date.
    topn = nonstandard_dpd_by_date(text)
    if topn:
        for a in accounts:
            d = a.get("date_of_sanction")
            if d in topn:
                # a["max_dpd"] may be None (grid unread) - this table resolves it
                a["max_dpd"] = max(a["max_dpd"] or 0, topn[d])

    accounts.sort(key=lambda x: x["sr_no"])
    return name, score, blocks, accounts, reported
