"""
crif_parser.py  -  CRIF High Mark CIBIL parser
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
    # PROV2: "Name: FULLNAME  DOB/Age: ..." (Inquiry Input Information section)
    m = re.search(r'\bName:\s+([A-Z][A-Z ]+?)\s+(?:DOB|Age|Gender)\b', text)
    if m:
        return re.sub(r'\s{2,}', ' ', m.group(1)).strip()
    # CRIF retail: "For NAME\n" or "For NAME CHM/Application/Credit"
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
    # Primary: score digit appears right after "300-900" on the same line
    m = re.search(r'300-900\s*\n?\s*(\d{3})\b', text)
    if m:
        v = int(m.group(1))
        if 300 <= v <= 900:
            return v
    # PROV2 fallback: "CB SCORE Enquired Entity exists in bureau 716" (Verification section)
    m = re.search(r'CB\s+SCORE[^\n]+\b(\d{3})\b', text, re.IGNORECASE)
    if m:
        v = int(m.group(1))
        if 300 <= v <= 900:
            return v
    # Last resort: any PERFORM line with a 3-digit value in range
    m = re.search(r'PERFORM[^\n]*?(\d{3})\b', text)
    if m:
        v = int(m.group(1))
        if 300 <= v <= 900:
            return v
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

    # PROV2 Account Summary columns:
    #   Number of Accounts | Active | Overdue | Secured | UnSecured | Untagged | Amounts...
    # Active is at index 1; Secured + UnSecured == Total (sanity check). The first
    # comma-amount on the same row is Total Current Balance (the remaining amounts
    # are its secured/unsecured split, disbursed, sanctioned, overdue).
    if totals["account_count"] is None:
        group_m   = re.search(r'Group\s+Account\s+Summary', section, re.IGNORECASE)
        main_part = section[: group_m.start()] if group_m else section
        for line in main_part.split('\n'):
            # Match standalone 1-3 digit numbers (exclude digits inside large comma amounts)
            small = [int(v) for v in re.findall(r'(?<![,\d])(\d{1,3})(?![,\d])', line)
                     if int(v) < 500]
            if len(small) >= 5:
                total, active, _, secured, unsecured = small[:5]
                if secured + unsecured == total and 0 < total < 500:
                    totals["account_count"] = active
                    amounts = re.findall(r'\d{1,3}(?:,\d{2,3})+', line)
                    if amounts:
                        totals["total_balance"] = to_int(amounts[0])
                    break

    return totals


# ─────────────────────────────────────────────────────────────────
# ACCOUNT BLOCK SPLITTING
# ─────────────────────────────────────────────────────────────────

_BLOCK_PATTERNS = [
    # P1: number on next line      "Account Information\n3\n"
    re.compile(r'Account\s+Information\s*\n\s*(\d{1,3})\s*\n',     re.MULTILINE),
    # P2: blank line before number "Account Information\n\n3\n"
    re.compile(r'Account\s+Information\s*\n\s*\n\s*(\d{1,3})\s*\n', re.MULTILINE),
    # P3: number on same line      "Account Information 3\n"
    re.compile(r'Account\s+Information\s+(\d{1,3})\s*\n',           re.MULTILINE),
    # P5: number inline with Account Type (HTML-to-PDF format)
    #     "Account Information\n20  Account Type: ..."
    re.compile(r'Account\s+Information\s*\n(\d{1,3})\s+Account\s+Type:', re.MULTILINE),
]

# P4: number appears on line BEFORE "Account Information\n\nAccount Type:"
# Requires blank line after header to avoid catching page numbers
_P4 = re.compile(
    r'(\d{1,3})\s*\n(Account\s+Information\s*\n\s*\n\s*Account\s+Type:)',
    re.MULTILINE,
)

# OCR sometimes corrupts the word "Account" in the block header  -  dropping the
# leading 'A' ("ccount Information") or splitting it ("Acco unt Information").
# Tolerate both so no account block is lost. The trailing \n keeps the appendix
# rows ("Account Information - Credit Grantor ...") from matching.
_AI_HEADER    = re.compile(r'A?cco\s?unt\s+Information\s*\n', re.MULTILINE)
_BLOCK_FIELD  = re.compile(
    r'Account\s+Type:|Disbursed\s+Date:|Current\s+Balance:|Credit\s+Grantor:'
    r'|\d{2}-\d{2}-\d{4}',   # DD-MM-YYYY date present in every real account block
    re.IGNORECASE,
)


def split_account_blocks(text: str) -> list:
    """
    Multi-pass splitter handling all known CRIF HTML-to-PDF format variants:
      P1/P2/P3  -  standard numbered blocks (number after header)
      P4         -  number appears on line BEFORE "Account Information" header
      P5         -  number on same line as Account Type after header
      Pass 2     -  page-break recovery (number swallowed by browser header)
    Returns list of (account_number: int, block_text: str).
    """
    candidates = []

    # P1/P2/P3/P5  -  block starts at "Account Information"
    for pat in _BLOCK_PATTERNS:
        for m in pat.finditer(text):
            candidates.append((m.start(), int(m.group(1))))

    # P4  -  number before header; block starts at "Account Information"
    for m in _P4.finditer(text):
        num    = int(m.group(1))
        ai_pos = m.start(2)   # position of "Account Information"
        candidates.append((ai_pos, num))

    if candidates:
        candidates.sort(key=lambda x: x[0])
        deduped = [candidates[0]]
        for pos, num in candidates[1:]:
            if pos - deduped[-1][0] > 30:
                deduped.append((pos, num))
        found_positions = {pos for pos, _ in deduped}
    else:
        # No P1-P5 match (e.g. PROV2 OCR where number/Account Type line is garbled).
        # Fall through to Pass 2 which discovers blocks by field presence alone.
        deduped, found_positions = [], set()
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


_DATE_RE      = re.compile(r'\d{2}-\d{2}-\d{4}')
# Dates that are NOT the disbursed date  -  exclude them in the fallback scan.
_DATE_EXCLUDE = re.compile(
    r'(?:Ason|Last\s+Payment\s+Date|Closed\s+Date|Last\s+Reported|as\s+of)\s*[:.]?\s*$',
    re.IGNORECASE,
)


def _extract_date(block: str) -> str:
    val = _next_line_value(block, "Disbursed Date:")
    if re.match(r'\d{2}-\d{2}-\d{4}', val):
        return val[:10]
    m = re.search(r'Disbursed\s+Date[:\s]+(\d{2}-\d{2}-\d{4})', block)
    if m:
        return m.group(1)
    m = re.search(r'Date\s+of\s+Sanction[:\s]+(\d{2}[-/]\d{2}[-/]\d{4})', block, re.IGNORECASE)
    if m:
        return m.group(1).replace("/", "-")
    # Fallback: row reconstruction sometimes splits the disbursed date onto the
    # line above its label (e.g. it lands on the Ownership row). Take the earliest
    # date in the block that isn't an Ason / Last-Payment / Closed / Reported date
    #  -  disbursement is the origination event, so it's the oldest.
    cands = []
    for dm in _DATE_RE.finditer(block):
        if not _DATE_EXCLUDE.search(block[max(0, dm.start() - 22): dm.start()]):
            cands.append(dm.group(0))
    if cands:
        return min(cands, key=lambda d: (d[6:10], d[3:5], d[0:2]))
    return "NA"


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


_OWNERSHIP_KW = re.compile(
    r'\b(INDIVIDUAL|GUARANTOR|JOINT|SINGLE|SOLE|CO-?BORROWER|PROPRIETOR)\b',
    re.IGNORECASE,
)


def _extract_ownership(block: str) -> str:
    """Read Ownership: field (INDIVIDUAL / GUARANTOR / JOINT / etc.)."""
    m = re.search(r'Ownership\s*:\s*([^\n]*)', block, re.IGNORECASE)
    if m:
        # Value on the same line as "Ownership:"
        kw = _OWNERSHIP_KW.search(m.group(1))
        if kw:
            return kw.group(1).title()
        # OCR sometimes drops the value here (status marker swallowed the slot);
        # strip the trailing newline first so split('\n')[0] gives the actual next line
        kw2 = _OWNERSHIP_KW.search(block[m.end():].lstrip('\n').split('\n')[0])
        if kw2:
            return kw2.group(1).title()

    # Fallback: OCR dropped the "Ownership:" label entirely.
    # The value still appears on the "Disbursed Date:" line (adjacent field).
    m2 = re.search(r'[^\n]*Disbursed\s+Date[^\n]*', block, re.IGNORECASE)
    if m2:
        kw = _OWNERSHIP_KW.search(m2.group(0))
        if kw:
            return kw.group(1).title()

    return ""


# Words that mark the end of a Credit Grantor value (the next column's label).
_ENTITY_STOP = re.compile(
    r'\b(?:Account|Lender|Ason|Disbursed|Disbd|Ownership|Type|Last|Closed|Cash)\b',
    re.IGNORECASE,
)


# Words that signal a genuine lender name (so a lone token like "SBI" survives).
_LENDER_KW = re.compile(
    r'BANK|FINANC|LIMITED|\bLTD\b|\bHFC\b|NBFC|HOUSING|CORP|CREDIT\s+CO|'
    r'SOCIET|FINSERV|CAPITAL|MAHINDRA|BAJAJ|MUTHOOT|MANNAPURAM',
    re.IGNORECASE,
)


def _is_masked_entity(val: str) -> bool:
    """
    Masked/undisclosed or garbage grantor → treat as NA. Real lender names have no
    digits, aren't 'XXXX', aren't a bled-in loan type, and are either multi-word or
    carry a lender keyword (so 'FED'-style OCR noise is rejected but 'HDFC BANK' is
    kept).
    """
    if not val:
        return True
    if re.search(r'\d', val):                      # real lender names carry no digits
        return True
    if re.search(r'X{2,}', val, re.IGNORECASE):
        return True
    upper = val.upper()
    if ('LOAN' in upper or 'OVERDRAFT' in upper or 'CREDIT CARD' in upper) \
            and not _LENDER_KW.search(val):        # a loan type bled into the column
        return True
    if len(re.findall(r'[A-Za-z]{2,}', val)) >= 2:
        return False
    return not _LENDER_KW.search(val)              # lone token: keep only if a lender word


def _extract_entity(block: str) -> str:
    """
    Read the account's OWN Credit Grantor (per-block  -  positional lists misalign).
    OCR writes the label as 'Credit Grantor:' / 'Grantor.' / 'Grantor', often with
    the value inline and the next column bleeding in. Masked grantors → 'NA'.
    """
    m = re.search(r'Credit\s+Grantor\s*[:.\-=]?\s*([^\n]*)', block, re.IGNORECASE)
    if not m:
        return "NA"
    val = m.group(1)
    stop = _ENTITY_STOP.search(val)
    if stop:
        val = val[: stop.start()]
    val = val.strip(" .:'`-*‘’�\t")
    return "NA" if _is_masked_entity(val) else val


# Canonical CRIF loan types, longest/most-specific first so greedy matching wins.
_LOAN_TYPES = [
    "CONSTRUCTION EQUIPMENT LOAN", "COMMERCIAL VEHICLE LOAN",
    "BUSINESS LOAN UNSECURED", "BUSINESS LOAN SECURED", "LOAN AGAINST PROPERTY",
    "AUTO LOAN (PERSONAL)", "KISAN CREDIT CARD", "TWO-WHEELER LOAN",
    "USED CAR LOAN", "CONSUMER LOAN", "PROPERTY LOAN", "PERSONAL LOAN",
    "HOUSING LOAN", "HOME LOAN", "TRACTOR LOAN", "EDUCATION LOAN", "GOLD LOAN",
    "OVERDRAFT", "BUSINESS LOAN", "AUTO LOAN", "CREDIT CARD",
]


def _squash(s: str) -> str:
    return re.sub(r'[^A-Z0-9]', '', s.upper())


_LOAN_SQUASHED = [(t, _squash(t)) for t in _LOAN_TYPES]


def _extract_loan_type(block: str) -> str:
    """
    Match the account type against the known CRIF vocabulary, comparing on a
    punctuation/whitespace-stripped form so OCR noise (hyphens, parens, split
    words, two-row label/value layout) doesn't break it. Falls back to the inline
    'Account Type:' value, else 'Unknown'.
    """
    head = block.split("Payment History")[0][:600]
    sq   = _squash(head)
    for canon, csq in _LOAN_SQUASHED:
        if csq in sq:
            return canon
    m = re.search(r'Account\s+Type\s*[:.\-=]?\s*([^\n]*)', head, re.IGNORECASE)
    if m:
        val = m.group(1)
        for stop in ("Credit Grantor", "Account #", "Lender Type", "Credit", "Account", "Ason"):
            i = val.find(stop)
            if i > 0:
                val = val[:i]
        val = val.strip(" .:'`-*�\t")
        # Reject label residue that leaked in (e.g. 'Credit Grantor: #') and require
        # a real word.
        if re.search(r'[A-Za-z]{3,}', val) and not re.search(
                r'Grantor|Lender|Credit|Account|#', val, re.IGNORECASE):
            return val
    return "Unknown"


# Frequency abbreviations that look like an asset class after a '/'. They come from
# the EMI field (e.g. '2,31,400/Monthly') and must NOT be read as DPD cells.
_DPD_FREQ = {"MON", "ANN", "MTH", "WK", "QTR", "QUA", "WEE", "HAL", "FOR", "BIM"}


def _extract_max_dpd(block: str) -> int:
    # DPD grid cells are "NNN/AssetClass" (e.g. 027/XXX). OCR mangles them two ways:
    #   - the days value loses leading zeros, so it can be 1-3 digits ('24/XXX');
    #   - the asset class is mis-read ('027/KXX' for '027/XXX'), so requiring an exact
    #     class silently dropped the cell.
    # Accept a 2-3 LETTER class (covers XXX/STD/SMA/... and garbles like KXX) but
    # reject digit-only tokens ('200/200') and frequency words ('400/Monthly'), which
    # would otherwise fabricate DPD from EMI amounts and '000'→'200' misreads.
    m      = re.search(r'Payment\s+History', block, re.IGNORECASE)
    region = block[m.end():] if m else block
    vals   = [
        int(num)
        for num, cls in re.findall(r'(?<!\d)(\d{1,3})\s*/\s*([A-Za-z]{2,3})', region)
        if cls.upper() not in _DPD_FREQ and int(num) < 900
    ]
    return max(vals) if vals else 0


def _is_closed(block: str) -> bool:
    """
    Rule 1: Closed Date has a valid date.
    Rule 2: Remarks contains 'Written-off'.
    Rule 3: Compact block  -  'Closed' before any field label.
    Rule 4: Total write-off amount field is non-zero.
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

    wo_m = re.search(
        r'(?:Total\s+)?Write\s*[- ]?[Oo]ff\s+Amt[:\s]*\n?\s*([\d,]+)',
        block, re.IGNORECASE,
    )
    if wo_m and to_int(wo_m.group(1)) != 0:
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
        raw  = m.group(1)
        stop = _ENTITY_STOP.search(raw)
        if stop:
            raw = raw[: stop.start()]
        entity = raw.strip(" .:'`-*‘’�\t")
        cg_list.append("NA" if _is_masked_entity(entity) else entity)

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
        "ownership":        _extract_ownership(block),
        "type_of_loan":     loan_type if loan_type else _extract_loan_type(block),
        "max_dpd":          _extract_max_dpd(block),
        "status":           "Closed" if _is_closed(block) else "Active",
    }


# ─────────────────────────────────────────────────────────────────
# MAIN CRIF PARSE  (called by parser.py orchestrator)
# ─────────────────────────────────────────────────────────────────

def parse_crif(text: str) -> tuple:
    """Returns (name, score, blocks, accounts, reported_totals)."""
    # The OCR layer injects commercial status-strip markers on every scanned page;
    # the retail path doesn't use them, so drop them before they can bleed into fields.
    text     = re.sub(r'__STATUS_(?:ACTIVE|CLOSED)__', '', text)
    name     = extract_name(text)
    score    = extract_score(text)
    reported = extract_reported_totals(text)
    blocks   = split_account_blocks(text)

    # Entity & loan type: hybrid source. The compact summary's positional lists are
    # the authoritative, clean source  -  but ONLY when they align 1:1 with the blocks
    # (true for digital/clean text). Under OCR garble the label count drifts (e.g. 34
    # blocks vs 13 labels), so there we fall back to per-block extraction. Some CRIF
    # variants don't even print Account Type in the detail block (only in the
    # summary), so the positional list is essential for those.
    at_list, cg_list = build_positional_lists(text)
    at_ok = len(at_list) == len(blocks)
    cg_ok = len(cg_list) == len(blocks)

    accounts = []
    for idx, (num, blk) in enumerate(blocks):
        lt = at_list[idx] if at_ok else None
        en = cg_list[idx] if cg_ok else None
        if not lt or lt == "Unknown":
            lt = _extract_loan_type(blk)
        if not en or en == "NA":
            en = _extract_entity(blk)
        accounts.append(extract_account(num, blk, lt, en))

    accounts.sort(key=lambda x: x["sr_no"])
    return name, score, blocks, accounts, reported
