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


def _parse_summary_row_full(row: str, lender_inline: bool) -> dict:
    """
    Full Borrower Summary row -  every column, not just Live Accts/Outstanding
    (see _parse_summary_row for the row-shape background). Missing fields are
    None rather than 0, so the analyst sees a blank instead of a fabricated
    zero when a column genuinely couldn't be read.
    """
    ints = re.findall(r'(?<![\d.])\d{1,5}(?![\d.])', row)
    lenders = total = live = delinquent = None
    if lender_inline and len(ints) >= 4:
        lenders, total, live, delinquent = (to_int(x) for x in ints[:4])
    elif not lender_inline and len(ints) >= 3:
        total, live, delinquent = (to_int(x) for x in ints[:3])

    # Live Accts can never exceed Total Accts - on scanned reports OCR
    # sometimes drops a leading digit from Total (e.g. misreads '17' as
    # '7'), which this would otherwise pass through as a self-contradictory
    # number. Null it out rather than show something the analyst would
    # immediately (correctly) distrust.
    if total is not None and live is not None and total < live:
        total = None

    # Sanctioned Amt precedes the '(x%)' token; Outstanding/Overdue/PAR(90+)
    # follow it in that order (all in Crores in the source text).
    amt_m = re.search(
        r'([\d]+\.?\d*)\s*\(\s*[\d.]+\s*%\s*\)\s*([\d]+\.?\d*)'
        r'(?:\s+([\d]+\.?\d*))?(?:\s+([\d]+\.?\d*))?',
        row,
    )
    sanctioned = outstanding = overdue = par_90plus = None
    if amt_m:
        sanctioned  = int(round(float(amt_m.group(1)) * _CRORE))
        outstanding = int(round(float(amt_m.group(2)) * _CRORE))
        if amt_m.group(3):
            overdue = int(round(float(amt_m.group(3)) * _CRORE))
        if amt_m.group(4):
            par_90plus = int(round(float(amt_m.group(4)) * _CRORE))

    return {
        "lenders":          lenders,
        "total_accts":      total,
        "live_accts":       live,
        "delinquent_accts": delinquent,
        "sanctioned_amt":   sanctioned,
        "outstanding_amt":  outstanding,
        "overdue_amt":      overdue,
        "par_90plus":       par_90plus,
    }


def extract_borrower_summary(text: str) -> dict:
    """
    Full Borrower Summary table (Your Institution vs Other Institution -
    what Shriram already lends this borrower vs the rest of the market),
    plus the adjacent Length of Credit History / Last-12-Months profile
    fields printed right below it. Surfaced to the analyst as a header
    block; extract_reported_totals() above still does the narrower
    Live-Accts/Outstanding pull used for validation math.

    Returns {} if the section can't be found (e.g. an older/scanned layout
    that doesn't carry it).
    """
    section = _summary_section(text)
    if not section:
        return {}

    flat = re.sub(r'\s+', ' ', section)
    lender_inline = bool(re.search(r'Lender.{0,30}Total\s+Accts', flat, re.IGNORECASE))

    def row_for(label):
        m = re.search(
            re.escape(label) + r'\s*\n(.*?)(?=\n(?:Your Institution|Other Institution)\b|\n\*\s*Only|\Z)',
            section, re.DOTALL,
        )
        if not m:
            return None
        row = re.sub(r'\s+', ' ', m.group(1)).strip()
        return _parse_summary_row_full(row, lender_inline)

    # On scanned reports the whole row (History value + the next 'Profile In
    # Last 12 Months' block) can land on one OCR'd line with no newline
    # between them - stop at that phrase too, not just at '\n', so the
    # capture doesn't swallow the next section. OCR sometimes drops the
    # leading word 'Profile' entirely, so match with or without it.
    history_m = re.search(
        r'Length\s+of\s+Credit\s+History\s*:?\s*\n?\s*(.+?)'
        r'(?:\n|\s*(?:Profile\s+)?In\s+Last\s+12\s+Months|\Z)',
        section, re.IGNORECASE,
    )
    new_m     = re.search(r'New\s+Accts\s*:?\s*\n?\s*(\d+)', section, re.IGNORECASE)
    closed_m  = re.search(r'Closed\s+Accts\s*:?\s*\n?\s*(\d+)', section, re.IGNORECASE)
    newdel_m  = re.search(r'New\s+Delinquent\s+Accts\s*:?\s*\n?\s*(\d+)', section, re.IGNORECASE)

    return {
        "your_institution":         row_for("Your Institution"),
        "other_institution":        row_for("Other Institution"),
        "length_of_credit_history": history_m.group(1).strip() if history_m else None,
        "new_accts_12m":            int(new_m.group(1)) if new_m else None,
        "closed_accts_12m":         int(closed_m.group(1)) if closed_m else None,
        "new_delinquent_accts_12m": int(newdel_m.group(1)) if newdel_m else None,
    }


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


# Single-field fallback for when the Payment History grid can't be read at all.
# 'DPD/Asset Classification:' is a single always-present label (one value, not a
# dense 12-month grid), so it's far more reliable to extract - but it's
# categorical (a bucket), not the grid's exact day-count. Mapped to a
# representative DPD using RBI's own SMA-staging convention (SMA-0: 1-30,
# SMA-1: 31-60, SMA-2: 61-90, Substandard/NPA: 90+) - which is also what the
# Excel gradient's colour bands (excel_generator._DPD_BANDS) were built
# around, so a classification-derived value still lands in the right colour.
_CLASSIFICATION_WORD = r'STANDARD|SPECIAL\s+MENTION\s+ACCOUNT\s*-?\s*\n?\s*(\d)?|SUB\s*-?\s*STANDARD|DOUBTFUL|LOSS'
_CLASSIFICATION_RE = re.compile(
    r'DPD\s*/\s*Asset\s*\n?\s*Classification\s*:\s*\n?\s*(' + _CLASSIFICATION_WORD + r')',
    re.IGNORECASE,
)
# Bare-keyword fallback: on scanned reports, OCR column-bleed routinely
# detaches the classification word from its own label entirely - it can land
# on the 'Type:' line instead, lose the 'DPD/Asset' prefix so only
# 'Classification:' survives, or have a garbled word wedged in between (e.g.
# 'DPD/Asset Classification: seats STANDARD'). The word itself still reliably
# survives OCR even when its label doesn't, so search for it directly within
# the account's own header region instead of requiring exact label adjacency.
_CLASSIFICATION_BARE_RE = re.compile(r'\b(' + _CLASSIFICATION_WORD + r')\b', re.IGNORECASE)


def _classification_to_dpd(val: str, level: str = None):
    val = val.upper()
    if 'STANDARD' in val and 'SUB' not in val:
        return 0
    if 'SPECIAL MENTION' in val:
        level = level or '0'
        return {'0': 1, '1': 31, '2': 61}.get(level, 1)
    if 'SUB' in val:
        return 91
    return 181   # DOUBTFUL / LOSS


def _classification_dpd(block: str):
    """Representative DPD from the DPD/Asset Classification label. Falls
    back to a bare-keyword search of the account's header region (before the
    12-month balance-history table, where the classification word actually
    lives even when detached from its label) if the label-anchored match
    fails. Returns None only when neither finds anything - grid AND field
    both genuinely unreadable."""
    m = _CLASSIFICATION_RE.search(block)
    if m:
        return _classification_to_dpd(m.group(1), m.group(2))
    head = block.split("Current Balance History")[0][:500]
    m = _CLASSIFICATION_BARE_RE.search(head)
    if not m:
        return None
    return _classification_to_dpd(m.group(1), m.group(2))


def _extract_max_dpd(block: str) -> int | None:
    """
    DPD from the Payment History grid ('NNN/SMA', 'NNN/xxx', 'NNN/STD', ...).

    Most CRIF Commercial reports use 'xxx' as the clean-cell class code, but
    some (confirmed on a real scanned report) print the literal 'STD'
    instead - both are treated the same way here.

    OCR routinely misreads the leading zeros of '000/xxx' (or '000/STD') as
    '200/xxx', which would fabricate delinquency. We guard against this by
    accepting those clean-cell classes only when DPD >= 10 (single-digit OCR
    drift from 000 is implausible at that magnitude). Named non-standard
    buckets (SMA/SUB/DBT/LOS) are trusted at any positive value. Vision
    fallback recovers precise DPD when OCR fails on colored cells.

    Returns None (not 0) when the grid pattern wasn't found at all in the
    block AND the DPD/Asset Classification field is also absent - that means
    the report gave us nothing to go on for this account. Callers render
    None as "Check CIBIL" instead of a silent 0.
    """
    raw_matches = list(re.finditer(r'\b\d{1,3}\s*/\s*(?:SMA|SUB|DBT|LOS|xxx|STD)\b', block, re.IGNORECASE))
    if not raw_matches:
        return _classification_dpd(block)

    vals = []
    # Named non-standard buckets: any positive value is real delinquency
    for m in re.finditer(r'\b(\d{1,3})\s*/\s*(?:SMA|SUB|DBT|LOS)\b', block, re.IGNORECASE):
        v = int(m.group(1))
        if 0 < v < 999:
            vals.append(v)
    # Clean-cell classes ('xxx' / 'STD'): accept when >= 10 to filter OCR noise on 000
    for m in re.finditer(r'\b(\d{1,3})\s*/\s*(?:xxx|STD)\b', block, re.IGNORECASE):
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
# as of:' at all (see _HTML_STATUS_BADGE) - instead the bureau's own
# colour-coded pill is printed as a bare word directly after the page footer
# ("...Confidential\n"), and EVERY trade's pill since the previous footer is
# stacked together there in the same document order as the trades themselves
# (not one pill immediately after each trade). Five pill values are observed
# in practice: ACTIVE / CLOSED / DELINQUENT (still open, flagged for overdue
# payment history - a subset of "live", not a closed-like status) / WRITTEN
# OFF / SETTLED. Anchoring on the footer (rather than the 'Loan Terms For'
# grouping used by split_account_blocks) avoids that grouping's failure mode
# where a report's cover-page boilerplate or a page-break-truncated account
# swallows a real trade into a badge-less span.
_STATUS_PILL_WORD    = r'CLOSED|ACTIVE|DELINQUENT|WRITTEN OFF|SETTLED'
_STATUS_PILL_CLUSTER = re.compile(
    r'Confidential\n((?:(?:' + _STATUS_PILL_WORD + r')\n)+)', re.MULTILINE
)

_PILL_STATUS_MAP = {
    "ACTIVE": "Active", "CLOSED": "Closed", "DELINQUENT": "Active",
    "WRITTEN OFF": "Written Off", "SETTLED": "Settled",
}


def _status_pill_map(text: str, trade_starts: list) -> tuple:
    """
    Maps trade index (0-based, matching `trade_starts` order) -> canonical
    status string, using the page-footer pill clusters (see module note
    above). Each cluster's pills belong to whichever trades were printed
    since the previous footer, in the same order - zippable 1:1 whenever a
    cluster's word count matches its trade count. A cluster that doesn't line
    up (e.g. a trade whose block was itself split across a page-break,
    losing a trade or a pill) contributes nothing for those trades; callers
    fall back to _resolve_status()'s per-block rules for them.

    Returns (status_map, delinquent_set) - delinquent_set holds the trade
    indices whose bureau pill was specifically 'DELINQUENT' (folded into
    'Active' in status_map, since a delinquent facility is still open, but
    kept separately so callers can still surface it to the analyst).
    """
    if not trade_starts:
        return {}, set()
    clusters = [(m.start(), m.group(1).strip().split('\n'))
                for m in _STATUS_PILL_CLUSTER.finditer(text)]
    if not clusters:
        return {}, set()

    status_map, delinquent = {}, set()
    prev_end = 0
    for cpos, words in clusters:
        trades_here = [i for i, t in enumerate(trade_starts) if prev_end <= t < cpos]
        if trades_here and len(trades_here) == len(words):
            for ti, word in zip(trades_here, words):
                status_map[ti] = _PILL_STATUS_MAP[word]
                if word == "DELINQUENT":
                    delinquent.add(ti)
        prev_end = cpos
    return status_map, delinquent


def _resolve_status(block: str, current_balance: int = None) -> str:
    """
    Per-block rule-based status - the fallback for trades _status_pill_map()
    couldn't resolve from the bureau's own pill. Same rule order as before,
    but discriminates the Closure Reason value into its specific terminal
    status (Written Off / Settled) instead of collapsing everything to
    "Closed".
    """
    # Rule 0 (primary): the bureau's own colour-coded status strip. For scanned
    # PDFs it's read from the left margin during OCR and injected as a marker;
    # for HTML sources the same badge is literal text right after 'Info. as
    # of: <date>' (a colour-coded pill in the rendered page). Most reliable
    # signal either way  -  the per-account Closure Reason/Closed Date fields
    # are usually blank even on accounts that are genuinely closed.
    if "__STATUS_CLOSED__" in block:
        return "Closed"
    if "__STATUS_ACTIVE__" in block:
        return "Active"
    m = _HTML_STATUS_BADGE.search(block)
    if m:
        return "Closed" if m.group(1).upper() == "CLOSED" else "Active"
    # Rule 1: the Closure Reason VALUE names a terminal status.
    reason = _field(block, r'Closure\s+(?:Reason|Status)')
    if reason:
        if re.search(r'WRITTEN\s*-?\s*OFF', reason, re.IGNORECASE):
            return "Written Off"
        if re.search(r'SETTLED', reason, re.IGNORECASE):
            return "Settled"
        if _CLOSED_KEYWORDS.search(reason):
            return "Closed"
    # Rule 2: a real Closed Date is present.
    if re.search(r'Closed\s+Date\s*[:.]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE):
        return "Closed"
    # Rule 3: a non-zero written-off amount.
    wo = re.search(r'Written\s+Off\s+Amt\s*[:.]?\s*([\d,]+)', block, re.IGNORECASE)
    if wo and to_int(wo.group(1)) > 0:
        return "Written Off"
    # Rule 4 (fallback, no strip / no explicit signal): the account is LIVE if it
    # still has money against it  -  outstanding balance > 0 OR an open drawing power
    # (a sanctioned-but-undrawn facility is still live). Only when both are zero do
    # we treat it as closed. Current balance is the figure that actually drives the
    # report total, so a misclassified zero-balance account costs nothing there.
    drawing_power = _amount(block, r'Drawing\s+Power')
    if (current_balance and current_balance > 0) or drawing_power > 0:
        return "Active"
    # Exception: reported on its very first day (Info. as of == Sanctioned Date)  -
    # newly originated, zero balance just means not yet drawn. Keep it live.
    if current_balance is not None and current_balance == 0:
        sanction_date = re.search(r'Sanctioned\s+Date\s*[:.]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE)
        info_as_of    = re.search(r'Info\.?\s*as\s*of\s*[:]?\s*(\d{2}-\d{2}-\d{4})', block, re.IGNORECASE)
        if sanction_date and info_as_of and sanction_date.group(1) == info_as_of.group(1):
            return "Active"  # brand-new account, not yet drawn
    return "Closed"


def _extract_suit_filed(block: str) -> bool:
    """True when 'Suit Filed Status:' reads exactly 'Suit Filed'. An exact
    match (not just "doesn't say Not") matters because 'Suit Amount:' isn't
    in the shared _STOP list: when the status value is blank, _field() reads
    straight through to that next label and returns "Suit Amount:" instead
    of "" - a loose 'not "not" in val' check would misread that bleed-through
    as a genuine Suit Filed flag."""
    val = _field(block, r'Suit\s*Filed\s*Status')
    return val.strip().upper() == 'SUIT FILED'


def extract_account(ordinal: int, block: str, scanned: bool = False) -> dict:
    balance = _amount(block, r'Current\s+Balance')
    # Fallback: scanned OCR sometimes can't read the Current Balance field but
    # CAN read Drawing Power (same value for active facilities). Use it when
    # balance is 0 and drawing power is present. Digital text reads a
    # genuine zero reliably (no OCR ambiguity), so this only applies when
    # the text came through OCR - on digital text a substituted balance would
    # overstate a correctly-read zero-utilisation facility.
    if scanned and balance == 0:
        dp = _amount(block, r'Drawing\s+Power')
        if dp > 0:
            balance = dp
    # Status uses the raw block (it carries the injected strip marker); all other
    # fields use a cleaned copy so the marker can't bleed into entity/loan type.
    # This is the RULE-BASED status only - the caller may still override it with
    # the bureau's own status pill (more authoritative). The active-zero -> None
    # conversion therefore can't happen here; it has to wait for that final,
    # authoritative status (see _apply_check_cibil_nulls), otherwise an account
    # this rule-based guess called "Active" gets its confidently-read zero
    # balance wiped out, and then the pill override reclassifies it as Closed -
    # leaving a Closed account nonsensically flagged "Check CIBIL".
    status = _resolve_status(block, balance)
    clean  = re.sub(r'__STATUS_(?:ACTIVE|CLOSED)__', '', block)
    sanction = _amount(clean, r'Sanctioned\s+Amount')
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
        "delinquent":       False,   # overridden by the caller from the pill map
        "suit_filed":       _extract_suit_filed(clean),
    }


# ─────────────────────────────────────────────────────────────────
# PORTFOLIO-LEVEL ANALYSIS  (derived from the extracted accounts, not
# re-parsed from the report's own summary tables)
# ─────────────────────────────────────────────────────────────────
#
# The report prints its own 'Credit Profile Summary' (asset-class grid) and
# 'Additional Status' (derog) tables, but both are frequently truncated by
# the same page-break issue documented throughout this file - the
# 'Additional Status' table in particular has been observed printing only
# its column headers with the data row swallowed entirely. Deriving these
# from the accounts list we've already extracted is self-consistent (same
# numbers the analyst sees in the account rows) and immune to that failure
# mode, at the cost of not matching the report's OWN "Facility Group"/
# "Institution" cross-tab dimensions - a reasonable trade for reliability.

# RBI's own SMA-staging convention - the same bands _classification_dpd()
# maps a report's classification word to, applied in reverse to bucket a
# numeric max_dpd back into a named asset class for the summary.
_ASSET_CLASS_BANDS = [
    (0,   "Standard"),
    (30,  "SMA-0"),
    (60,  "SMA-1"),
    (90,  "SMA-2"),
    (180, "Substandard"),
]


def _asset_class(dpd) -> str:
    if dpd is None:
        return "Unclassified"
    for cap, label in _ASSET_CLASS_BANDS:
        if dpd <= cap:
            return label
    return "Doubtful/Loss"


_ASSET_CLASS_ORDER = [lbl for _, lbl in _ASSET_CLASS_BANDS] + ["Doubtful/Loss", "Unclassified"]


def credit_profile_summary(accounts: list) -> list:
    """
    Asset-class distribution (Standard/SMA-0/SMA-1/SMA-2/Substandard/
    Doubtful-Loss) of Active accounts, bucketed by max_dpd. Returns a list
    of {asset_class, count, outstanding} in canonical severity order,
    omitting empty buckets.
    """
    buckets = {}
    for a in accounts:
        if a.get("status") != "Active":
            continue
        cls = _asset_class(a.get("max_dpd"))
        b = buckets.setdefault(cls, {"count": 0, "outstanding": 0})
        b["count"] += 1
        b["outstanding"] += a.get("current_balance") or 0
    return [
        {"asset_class": cls, **buckets[cls]}
        for cls in _ASSET_CLASS_ORDER if cls in buckets
    ]


def derog_summary(accounts: list) -> dict:
    """
    Rollup of red-flag statuses (Written Off / Settled / Suit Filed /
    Delinquent) across all extracted accounts - count and total amount per
    category. A single account can appear in more than one bucket (e.g.
    Written Off AND Suit Filed).

    Written Off and Suit Filed use sanction_amount, not current_balance: the
    bureau zeroes current_balance once an account is written off or sued, so
    summing it would show a misleading "Rs.0 impact" for accounts that may
    carry crores of original exposure. Settled and Delinquent use
    current_balance because it's still meaningful there - Settled retains a
    real settlement balance, and Delinquent accounts are still open.
    """
    def bucket(pred, amount_field="current_balance"):
        matched = [a for a in accounts if pred(a)]
        return {"count": len(matched),
                "amount": sum(a.get(amount_field) or 0 for a in matched)}

    return {
        "written_off": bucket(lambda a: a["status"] == "Written Off", "sanction_amount"),
        "settled":     bucket(lambda a: a["status"] == "Settled"),
        "suit_filed":  bucket(lambda a: a.get("suit_filed"), "sanction_amount"),
        "delinquent":  bucket(lambda a: a.get("delinquent")),
    }


# ─────────────────────────────────────────────────────────────────
# MAIN PARSE  (called by parser.py orchestrator)
# ─────────────────────────────────────────────────────────────────

def _is_phantom(a: dict) -> bool:
    """Digital PDFs produce orphan fragments (balance-history columns split off
    by a repeated 'Loan Terms For' page header) with no extractable date,
    sanction amount, or balance — not real accounts."""
    return a["date_of_sanction"] == "NA" and a["sanction_amount"] == 0 and a["current_balance"] == 0


def _expand_account_blocks(text: str, trade_starts: list, status_map: dict,
                           delinquent_set: set, scanned: bool = False) -> tuple:
    """
    Per-group account extraction that fixes the case where a page break
    rendered 2+ trades' data inside a single 'Loan Terms For' group (see
    _status_pill_map) — extract_account()'s regex fields only match
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
            a = extract_account(ordinal, blk, scanned)
            if trades_here:
                ti = trades_here[0]
                if ti in status_map:
                    a["status"] = status_map[ti]
                a["delinquent"] = ti in delinquent_set
            blocks.append((ordinal, blk))
            accounts.append(a)
            continue
        for ti in trades_here:
            ordinal += 1
            t_start = trade_starts[ti]
            t_end   = trade_starts[ti + 1] if ti + 1 < len(trade_starts) else len(text)
            blk = text[t_start:t_end]
            a = extract_account(ordinal, blk, scanned)
            if not a["ownership"]:
                a["ownership"] = _ownership_before(text, t_start)
            if ti in status_map:
                a["status"] = status_map[ti]
            a["delinquent"] = ti in delinquent_set
            blocks.append((ordinal, blk))
            accounts.append(a)
    return blocks, accounts


def _apply_check_cibil_nulls(accounts: list, scanned: bool) -> None:
    """
    On scanned (OCR'd) reports, a literal 0 for Sanctioned Amount or Current
    Balance on an Active account is usually an unread field (0 is never a
    real sanctioned amount, and OCR often can't read Current Balance at all)
    - flip it to None so the app shows "Check CIBIL" instead of a fabricated
    0. On digital text there's no such OCR ambiguity: a literal 0 there is a
    confident, genuine read (e.g. an undrawn revolving facility), so it's
    left as-is. Runs after every status override (pill map, trade-vs-group
    quality pick) so it keys off each account's FINAL status, not the
    rule-based guess extract_account() started with.
    """
    if not scanned:
        return
    for a in accounts:
        if a["status"] != "Active":
            continue
        if a["sanction_amount"] == 0:
            a["sanction_amount"] = None
        if a["current_balance"] == 0:
            a["current_balance"] = None


def parse_crif_commercial(text: str, scanned: bool = False) -> tuple:
    """Returns (name, score, blocks, accounts, reported_totals, analysis)."""
    name     = extract_name(text)
    score    = extract_score(text)
    reported = extract_reported_totals(text)

    trade_starts = [m.start() for m in _TRADE_MARKER.finditer(text)]
    status_map, delinquent_set = _status_pill_map(text, trade_starts)

    blocks, accounts = _expand_account_blocks(text, trade_starts, status_map,
                                              delinquent_set, scanned)
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
            a = extract_account(num, blk, scanned)
            if not a["ownership"]:
                a["ownership"] = _ownership_before(text, start)
            if i in status_map:
                a["status"] = status_map[i]
            a["delinquent"] = i in delinquent_set
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

    _apply_check_cibil_nulls(accounts, scanned)

    accounts.sort(key=lambda x: x["sr_no"])

    analysis = {
        "borrower_summary":        extract_borrower_summary(text),
        "credit_profile_summary":  credit_profile_summary(accounts),
        "derog_summary":           derog_summary(accounts),
    }

    return name, score, blocks, accounts, reported, analysis
