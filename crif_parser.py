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


# PAN must carry the report's own "[PAN]" tag (e.g. "ABCPE1234F [PAN]" in
# the Inquiry Input Information ID(s) field) - matched against that specific
# labelled format only, never a bare PAN-shaped token found anywhere else in
# the report, which could just as easily belong to a co-applicant listed in
# a later table. DOB and phone are similarly anchored to their one specific
# header field, not the later "DOB Variations" / "Phone Variations" tables
# (multiple candidate values, no way to know which is current).
_PAN_RE = re.compile(r'([A-Z]{5}\d{4}[A-Z])\s*\[PAN\]')
# CRIF Commercial prints the borrower's own PAN as a plain "PAN:" label inside
# the Borrower Details header and tags nobody there with [PAN]; the [PAN] tags
# appear further down, on the directors and shareholders in Relationship
# Details. So the rule above, which is correct for a Retail report, picks the
# first director it meets. Seen on a real Commercial report for a company
# borrower: the header read "PAN: <company PAN>" and the [PAN] search
# returned a shareholder's PAN instead. The labelled header is tried first
# and the [PAN] tag remains the Retail path.
_BORROWER_PAN_RE = re.compile(
    r'Borrower\s+Details.*?\bPAN:\s*([A-Z]{5}\d{4}[A-Z])', re.S
)
_DOB_RE = re.compile(r'DOB/Age:\s*(\d{2}-\d{2}-\d{4})')
_PHONE_RE = re.compile(r'Phone\s*Numbers?:\s*(\+?\d[\d\s]{7,14}\d)')


def extract_borrower_identity(text: str) -> dict:
    """Best-effort PAN, date of birth, and primary phone number from the
    Inquiry Input Information header block. Each is matched against its own
    specific, labelled format only - a miss returns None rather than a
    wrong value, same never-guess contract as extract_name.
    """
    pan_match = _BORROWER_PAN_RE.search(text) or _PAN_RE.search(text)
    dob_match = _DOB_RE.search(text)
    phone_match = _PHONE_RE.search(text)
    return {
        "pan": pan_match.group(1) if pan_match else None,
        "dob": dob_match.group(1) if dob_match else None,
        "phone": phone_match.group(1).replace(" ", "") if phone_match else None,
    }


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
    Parse the CRIF Account Summary 12-column table:
      0 Number of Accounts   1 Active Accounts     2 Overdue Accounts
      3 Secured Accounts     4 UnSecured Accounts  5 Untagged Accounts
      6 Total Current Balance  7 Current Balance Secured  8 Current Balance Unsecured
      9 Total Sanctioned Amount  10 Total Disbursed Amount  11 Total Amount Overdue
    """
    totals = {"account_count": None, "total_balance": None,
              "total_sanction": None, "total_overdue": None}

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
        if len(nums) >= 12:
            totals["account_count"]  = nums[1]
            totals["total_balance"]  = nums[6]
            totals["total_sanction"] = nums[9]
            totals["total_overdue"]  = nums[11]
            return totals
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

    # PROV2 Account Summary columns (OCR dropped the header words but the data
    # row survives - seen on scanned reports where the primary "Total Amount
    # Overdue" header regex above can't match because OCR also scrambled the
    # header word order):
    #   Number of Accounts | Active | Overdue | Secured | UnSecured | Untagged |
    #   Total Current Balance | Current Balance Secured | Current Balance Unsecured |
    #   Total Sanctioned Amount | Total Disbursed Amount | Total Amount Overdue
    # Active is at index 1; Secured + UnSecured == Total (sanity check). The
    # comma-amounts on the same row follow that same 6-amount column order,
    # so amounts[0]=balance, amounts[3]=sanctioned, amounts[5]=overdue.
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
                    if len(amounts) >= 6:
                        totals["total_sanction"] = to_int(amounts[3])
                        totals["total_overdue"]  = to_int(amounts[5])
                    break

    # Layout where the summary's 12 header labels print as their own block
    # (not adjoining their values, the shape every fallback above assumes)
    # and the final header word can itself be print/OCR-truncated (confirmed
    # on a real report: "Total Amou\nOverdue\n" for "Total Amount Overdue"),
    # so even the anchor-token search at the top of this function can't
    # recognise it and the account-count validation check silently never
    # runs. The values still follow, one bare number per line, right after
    # that last header fragment - anchor on the LAST bare "Overdue" line in
    # the section (the final of the 12 headers is always "...Overdue"; an
    # earlier "Overdue Accounts" header line, if also split, still carries
    # the word "Accounts" too and won't match this bare-word-only pattern)
    # and read the next 12 standalone-number lines as the 12 columns in order.
    if totals["account_count"] is None:
        group_m   = re.search(r'Group\s+Account\s+Summary', section, re.IGNORECASE)
        main_part = section[: group_m.start()] if group_m else section
        last_overdue_line = None
        for hm in re.finditer(r'(?m)^[^\S\n]*Overdue[^\S\n]*$', main_part, re.IGNORECASE):
            last_overdue_line = hm
        if last_overdue_line:
            vals = []
            for line in main_part[last_overdue_line.end():].split('\n'):
                stripped = line.strip()
                if not stripped:
                    continue
                if re.fullmatch(r'[\d,]+', stripped):
                    vals.append(to_int(stripped))
                    if len(vals) >= 12:
                        break
                elif vals:
                    break
            if len(vals) >= 12:
                totals["account_count"]  = vals[1]
                totals["total_balance"]  = vals[6]
                totals["total_sanction"] = vals[9]
                totals["total_overdue"]  = vals[11]

    # Total Current Balance recovery when the row's own small integer columns
    # (Number/Active/Overdue Accounts) failed the secured+unsecured==total
    # sanity check above - that check catches OCR leading-digit drops (12->2)
    # in the COUNT columns, but the amount columns are unaffected by it and
    # can still be trusted. CRIF wraps a long "Total Current Balance" value
    # onto its own line when the row is wide, immediately followed by the
    # rest of the row's numbers - that adjacency is what "extracted_balance
    # over extracted_count" (this project's authoritative validation signal)
    # needs, even when the count half of the row is unreadable.
    if totals["total_balance"] is None:
        group_m   = re.search(r'Group\s+Account\s+Summary', section, re.IGNORECASE)
        main_part = section[: group_m.start()] if group_m else section
        m = re.search(r'\n\s*([\d,]{7,})\s*\n[^\n]*[\d,]{6,}', main_part)
        if m:
            totals["total_balance"] = to_int(m.group(1))

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

# OCR can drop the word "Account" from the header entirely (not just corrupt
# it - see _AI_HEADER above), leaving a bare "Information" line. Confirmed on
# a real report where a newer Tesseract build (5.5.0) read this same block
# more poorly than an older one (5.4.0) did, dropping "Account" outright and
# mangling "Account Type:" down to "ype: #:" on the next line - exactly the
# kind of OCR-quality difference that shifts the block count between
# environments running identical code. "Information" alone also appears
# elsewhere in the document (e.g. "Inquiry Input Information"), so this is
# only safe to treat as a header start when the very next line still carries
# a garbled fragment of "Type:" - that combination doesn't occur outside a
# real account block's header.
_BARE_INFO_HEADER = re.compile(r'\bInformation\s*\n(?=[^\n]*ype\s*:)', re.MULTILINE | re.IGNORECASE)

# OCR can also truncate "Information" itself, losing the trailing "-tion"
# ("Account Informati", "Account Informa") rather than corrupting "Account".
# Confirmed on a real scanned report where this exact truncation, combined
# with a mangled "Account"/"ccount"/"unt" prefix, silently dropped 16 of 62
# real accounts (the header matched none of the patterns above, so the whole
# account's fields got appended onto the tail of the previous block instead
# of starting a new one). The line is short (just the fragment) and unique to
# this header - "informa" doesn't otherwise appear on its own line anywhere
# else in a real report - so match a whole short line ending in "informa"
# (optionally +"ti", optionally +":") with nothing after it but the newline.
_AI_HEADER_TRUNCATED = re.compile(r'^.{0,12}informa(?:ti)?:?[ \t]*\n', re.MULTILINE | re.IGNORECASE)

# Pass 3: page-break recovery where the ENTIRE "Account Information" header
# text (not just the number, unlike Pass 2) is swallowed by the browser's
# print footer/header, leaving a bare number line directly followed by
# "Account Type:" with nothing in between - e.g.:
#   ...Confidential\n\n32\nAccount Type:\nCOMMERCIAL VEHICLE LOAN\n...
_BARE_NUM_BLOCK = re.compile(r'\n(\d{1,3})\s*\nAccount\s+Type:\s*\n', re.MULTILINE)


def split_account_blocks(text: str) -> list:
    """
    Multi-pass splitter handling all known CRIF HTML-to-PDF format variants:
      P1/P2/P3  -  standard numbered blocks (number after header)
      P4         -  number appears on line BEFORE "Account Information" header
      P5         -  number on same line as Account Type after header
      Pass 2     -  page-break recovery (number swallowed by browser header)
      Pass 3     -  page-break recovery (entire header text swallowed, only the
                    bare number survives directly before "Account Type:")
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
    # Pass 2 (header corrupted but still recognisable) and Pass 2b (header
    # lost the word "Account" entirely - see _BARE_INFO_HEADER) both infer
    # their ordinal from "how many blocks come before this position", so
    # their candidate positions must be collected FIRST and numbered
    # together in one position-sorted pass. Numbering each regex's matches
    # incrementally in two separate passes can give two genuinely different
    # accounts the same inferred number whenever a Pass 2b position lands
    # between two Pass 2 positions (each pass numbers against a different
    # partial snapshot of `deduped`) - and the same-number merge step below
    # would then wrongly treat them as one account reprinted across a page
    # break, silently dropping a real account.
    pass2_positions = []
    for pat in (_AI_HEADER, _BARE_INFO_HEADER, _AI_HEADER_TRUNCATED):
        for m in pat.finditer(text):
            pos = m.start()
            if any(abs(pos - fp) < 50 for fp in found_positions):
                continue
            if not _BLOCK_FIELD.search(text[pos: pos + 1000]):
                continue
            if not any(abs(pos - p) < 50 for p in pass2_positions):
                pass2_positions.append(pos)

    for pos in sorted(pass2_positions):
        prev_nums = [n for p, n in deduped if p < pos]
        prev_num  = max(prev_nums) if prev_nums else 0
        deduped.append((pos, prev_num + 1))
        found_positions.add(pos)

    # Pass 3  -  bare number directly before "Account Type:", no header text
    # at all (see _BARE_NUM_BLOCK). The number itself is trustworthy here (it
    # survived), so use it as-is rather than inferring by ordinal position.
    for m in _BARE_NUM_BLOCK.finditer(text):
        pos = m.start(1)
        if any(abs(pos - fp) < 50 for fp in found_positions):
            continue
        num = int(m.group(1))
        deduped.append((pos, num))
        found_positions.add(pos)

    deduped.sort(key=lambda x: x[0])

    # Merge consecutive entries that share the same account number - a real
    # account whose header lands right at a page bottom sometimes prints just
    # a stub (number + a couple of fields) before the page cuts it off, then
    # reprints its own number at the top of the next page followed by the
    # rest of its fields (this is exactly what Pass 3 is built to catch).
    # That reprint isn't a new account; keeping both entries would count the
    # same account twice. CRIF account numbers are unique per report, so any
    # adjacent duplicate is this continuation pattern, not a coincidence -
    # drop the second entry and let the merged block span both halves.
    # Guard: a report where none of the header patterns above matched
    # anything (a genuinely account-free report, or a format variant this
    # parser doesn't recognize) leaves deduped empty. Nothing to merge in
    # that case; the caller (split_account_blocks) already returns []
    # correctly for an empty deduped list further down, this just avoids
    # indexing into it here first.
    if deduped:
        merged = [deduped[0]]
        for pos, num in deduped[1:]:
            if num == merged[-1][1]:
                continue
            merged.append((pos, num))
        deduped = merged

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


# Known CRIF field labels  -  used to detect the "value vanished at a browser
# print page-break" signature: a label whose very next line is itself one of
# these labels (instead of a value) means the real value was swallowed by the
# page break (same root cause as split_account_blocks' Pass 3), not that the
# field is genuinely blank. Fields that are legitimately blank most of the
# time (Credit Limit, Cash Limit, Write off Date, ...) print no value line at
# all rather than skipping straight to the next label, so this check doesn't
# false-positive on them.
_KNOWN_LABELS = {
    "Account Type:", "Credit Grantor:", "Account #:", "Lender Type:", "Ownership:",
    "Disbursed Date:", "Disbd Amt/High Credit:", "Credit Limit:", "Last Payment Date:",
    "Current Balance:", "Cash Limit:", "Closed Date:", "Last Paid Amt:", "InstlAmt/Freq:",
    "Tenure(month):", "Overdue Amt:", "Write off Date:", "Account in Dispute:",
    "Account Remarks:", "Income/Freq:", "Principal Writeoff Amt", "Settlement Amt:",
    "Interest Rate:", "Total Writeoff Amt:", "Occupation:",
    "Payment History/Asset Classification:", "Collateral/Security Details:",
}


def _value_swallowed(block: str, label: str) -> bool:
    """True when `label`'s value line was eaten by a page break (the next
    non-blank line is itself a known field label, not a value)."""
    return _next_line_value(block, label) in _KNOWN_LABELS


def _extract_sanction_amt(block: str):
    val = _next_line_value(block, "Disbd Amt/High Credit:")
    if re.match(r'[\d,]+', val):
        return to_int(val.split()[0])
    m = re.search(r'Disbd\s+Amt[/\s](?:High\s+Credit)?[:\s]*([\d,]+)', block, re.IGNORECASE)
    if m:
        return to_int(m.group(1))
    if _value_swallowed(block, "Disbd Amt/High Credit:"):
        return None
    return 0


def _extract_balance(block: str):
    val = _next_line_value(block, "Current Balance:")
    if re.match(r'-?[\d,]+', val):
        return to_int(val.split()[0])
    m = re.search(r'Current\s+Balance[:\s]*(-?[\d,]+)', block, re.IGNORECASE)
    if m:
        return to_int(m.group(1))
    if _value_swallowed(block, "Current Balance:"):
        return None
    return 0


def _extract_overdue(block: str) -> int:
    val = _next_line_value(block, "Overdue Amt:")
    if re.match(r'[\d,]+', val):
        return to_int(val.split()[0])
    m = re.search(r'Overdue\s+(?:Amt)?[:\s]*([\d,]+)', block, re.IGNORECASE)
    if m:
        return to_int(m.group(1))
    # OCR sometimes wraps the value onto the line BEFORE the label instead
    # of after (column reflow drags a SECOND number onto the tail of
    # Tenure's own row, e.g. "...Tenure(month): 60  29,661\nOverdue Amt:\n" -
    # the trailing 29,661 is Overdue's value, 60 is still Tenure's own).
    # Must require BOTH numbers - Tenure's own value AND the spillover one -
    # not just "whatever number precedes the label". A blank Overdue Amt:
    # field (common, legitimate: the label just has nothing after it) leaves
    # only Tenure's own single value sitting right before the label, and a
    # looser match steals that instead of correctly returning 0 (confirmed
    # on two real reports - one HTML-exported, one native PDF, neither
    # scanned/OCR'd at all, so this can't be gated on `scanned`).
    lm = re.search(
        r'Tenure\s*\(month\)\s*:\s*[\d,]+\s+([\d,]+)\s*\n\s*Overdue\s+Amt\s*:',
        block, re.IGNORECASE)
    if lm:
        return to_int(lm.group(1))
    return 0


def _extract_emi(block: str) -> int:
    # Usually "6,758/Monthly" (amount/frequency), but some bureau-supplied
    # rows omit the frequency word entirely and print just "6,758" - the
    # amount is still real and still due, so it must not be dropped for
    # lack of a trailing "/frequency" to match against.
    val = _next_line_value(block, "InstlAmt/Freq:")
    m = re.match(r'([\d,]+)', val)
    if m:
        return to_int(m.group(1))
    m = re.search(r'InstlAmt/Freq[:\s]*([\d,]+)', block)
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

    # Header corrupted badly enough that OCR wedged garbage between the
    # type's own words (e.g. "COMMERCIAL VEHICLE cist LOAN") or lost the
    # "Account Type:" label entirely (its words landed mid-sentence with
    # other bled-in columns, e.g. "COMMERCIAL Credit Grantor: # Lender
    # Type: VEHICLE LOAN"), so neither the contiguous squash match above
    # nor the label-anchored regex below can find it. Fall back to a
    # fuzzy in-order match: all of a canonical type's words must still
    # appear in the head, in order, each within a bounded gap of the
    # next - loose enough to survive bled-in column text, tight enough
    # that it can't cross into an unrelated sentence.
    for canon in _LOAN_TYPES:
        words = re.findall(r'[A-Za-z]+', canon)
        if len(words) < 2:
            continue
        pattern = r'\b' + r'\b.{0,40}?\b'.join(re.escape(w) for w in words) + r'\b'
        if re.search(pattern, head, re.IGNORECASE | re.DOTALL):
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

# Some CRIF Retail grids print the DAYS half of the cell itself as a letter
# placeholder ("XXX/STD") instead of a digit count - "data not reported by
# institution" per the report's own appendix, distinct from a genuine 0.
# crif_commercial_parser.py already solved the equivalent problem for
# Commercial reports (_classification_to_dpd); mirrored here at the same
# representative-DPD bands (RBI's own SMA-staging convention) so a bare
# named class still lands in roughly the right colour bucket instead of
# silently reading as a clean 0 - which would hide a real SUB/DBT/LOS
# account behind a green cell.
_LETTER_DPD_MAP = {"STD": 0, "SMA": 1, "SUB": 91, "DBT": 181, "LOS": 181}
_LETTER_DPD_RE  = re.compile(r'\bXXX\s*/\s*(STD|SMA|SUB|DBT|LOS)\b', re.IGNORECASE)


def _letter_placeholder_dpd(region: str):
    """Representative DPD from 'XXX/CLASS' cells (no digit days at all)."""
    vals = [_LETTER_DPD_MAP[m.group(1).upper()] for m in _LETTER_DPD_RE.finditer(region)]
    return max(vals) if vals else None


_BLOCK_END_MARKERS_RE = re.compile(
    r'-END OF REPORT-|\nAppendix\n|\nInquiries\s*\(', re.IGNORECASE
)


def _payment_history_region(block: str) -> str:
    """
    The text from 'Payment History' to the end of the block - trimmed at
    the document-level 'Inquiries (past N months)' section, the
    end-of-report marker, or 'Appendix', whichever comes first. Only the
    report's LAST account has no next 'Account Information' header to
    bound its block, so its captured text can run on into all three -
    each digit-heavy (inquiry dates/amounts, timestamps, page numbers),
    which fooled the digit-density "genuinely blank vs. garbled"
    heuristic below into reporting a confidently-blank account as
    unreadable (confirmed on a real report: a small, already-closed loan
    with a genuinely empty Payment History section, misread as
    unable_to_verify solely because of this trailing document-level text
    bleeding into its block).
    """
    m = re.search(r'Payment\s+History', block, re.IGNORECASE)
    region = block[m.end():] if m else block
    end_m = _BLOCK_END_MARKERS_RE.search(region)
    return region[:end_m.start()] if end_m else region


def _extract_max_dpd(block: str, pre_scoped: bool = False):
    # DPD grid cells are "NNN/AssetClass" (e.g. 027/XXX). OCR mangles them two ways:
    #   - the days value loses leading zeros, so it can be 1-3 digits ('24/XXX');
    #   - the asset class is mis-read ('027/KXX' for '027/XXX'), so requiring an exact
    #     class silently dropped the cell.
    # Accept a 2-3 LETTER class (covers XXX/STD/SMA/... and garbles like KXX) but
    # reject digit-only tokens ('200/200') and frequency words ('400/Monthly'), which
    # would otherwise fabricate DPD from EMI amounts and '000'→'200' misreads.
    #
    # pre_scoped=True skips the "Payment History" label search entirely - for a
    # block obtained by clipping the PDF to this account's own geometric bounds
    # (see parser._account_clip_text), the label can legitimately appear AFTER
    # the grid values in document reading order (confirmed on a real report:
    # the account's mini-header + grid render before its own field labels in
    # the content stream), which would make _payment_history_region slice past
    # all the real data and report a false empty/unreadable region.
    region = block if pre_scoped else _payment_history_region(block)
    vals   = [
        int(num)
        for num, cls in re.findall(r'(?<!\d)(\d{1,3})\s*/\s*([A-Za-z]{2,3})', region)
        if cls.upper() not in _DPD_FREQ and int(num) < 900
    ]
    if vals:
        return max(vals)
    letter_dpd = _letter_placeholder_dpd(region)
    if letter_dpd is not None:
        return letter_dpd
    # No "NNN/class" cell recognised at all. A genuinely blank grid (brand
    # new account, no history yet) has almost no digits in this region; an
    # account with a real, populated grid that OCR garbled past recognition
    # (every slash misread, e.g. "083K" instead of "083/XXX") still carries
    # plenty of digit clutter (day-codes, year labels). Use that density as
    # the signal to report "genuinely unreadable" (None -> "Check CIBIL")
    # instead of silently fabricating a confident zero.
    if len(re.findall(r'\d', region)) >= 6:
        return None
    return 0


_YEAR_RE = re.compile(r'(?<![\d-])(20\d{2})\s*\n')
_CELL_RE = re.compile(r'((?:\d{1,3}|XXX)\s*/\s*[A-Za-z]{2,3}|-)', re.IGNORECASE)


def _cell_to_dpd(token: str):
    """One grid cell -> its DPD reading, or None if the cell itself is
    unreadable/blank ('-' = not yet elapsed / no data that month)."""
    token = token.strip()
    if token == '-':
        return None
    m = re.match(r'(\d{1,3}|XXX)\s*/\s*([A-Za-z]{2,3})', token, re.IGNORECASE)
    if not m:
        return None
    days_part, cls = m.group(1), m.group(2).upper()
    if cls in _DPD_FREQ:
        return None
    if days_part.upper() == 'XXX':
        return _LETTER_DPD_MAP.get(cls)
    # Aligned with _extract_max_dpd's own >= 900 floor: both functions read
    # the same "NNN/class" grid-cell format, and a >= 900 day count is
    # overwhelmingly a leading-zero OCR misread (e.g. "000" -> "900") rather
    # than a real value, on this reader as much as on that one. Treating the
    # two functions differently on the same input previously let the
    # accounts table show a clean max_dpd (rejected as noise) while this
    # window reader accepted the same cell as real and demanded RBH/ZCC
    # deviation approval for it - a contradiction with nothing on screen to
    # explain it. Reject here too rather than silently diverging.
    if int(days_part) >= 900:
        return None
    return int(days_part)


def _extract_dpd_window(block: str, pre_scoped: bool = False):
    """
    Reads the payment-history grid in true chronological order
    (most-recent-first) to derive:
      - last_reported_dpd: the most recently populated month's DPD
      - max_dpd_12mo: the worst DPD across the trailing 12 populated months
      - max_dpd_alltime: the worst DPD across the entire visible grid,
        preferred over _extract_max_dpd's flat whole-block scan when
        available (see why below)

    CRIF prints year blocks most-recent-year-first, each with 12 month
    cells left-to-right (Jan..Dec); reading a year block's cells in
    reverse (Dec..Jan) and walking year blocks top-to-bottom therefore
    yields cells in most-recent-first chronological order. A repeated
    year label marks a stale duplicate grid (seen on a real corrupted/
    merged block) and stops the scan.

    calendar_positions counts actual non-blank calendar months walked -
    this drives where the 12-month boundary falls, not the count of
    resolved values. A real report confirmed why: a cell like "XXX/XXX"
    (present, but not a recognised classification - _cell_to_dpd returns
    None for it) was being skipped without counting, so the walk reached
    into a 13th+ calendar month to make up the difference, silently
    widening the 12-month window (the same class of bug independently
    found and fixed on the CRIF Commercial ACE side of this codebase).
    Blank ('-') cells are the only thing skipped without counting - they
    mean no data that month, not "data present but unclassified".

    max_dpd_alltime exists because _extract_max_dpd only falls back to a
    letter-placeholder ('XXX/SUB' etc) reading when NO numeric cell exists
    anywhere in the block at all - so a block with a mix of real numeric
    readings AND a worse letter-placeholder reading (e.g. one month shows
    'XXX/SUB' - Substandard, no exact day-count printed - while other
    months show low numeric DPD) silently reports only the numeric max,
    missing the more severe classification entirely (confirmed on a real
    report: an account with a genuine 'XXX/SUB' reading and a numeric max
    of 27 reported max_dpd=27, when the true worst reading was 91 - the
    Substandard bucket's representative value).

    Returns (None, None, None) if no grid data could be read at all.

    pre_scoped=True skips the "Payment History" label search - see
    _extract_max_dpd's matching note for why a geometry-clipped block needs this.
    """
    region = block if pre_scoped else _payment_history_region(block)
    year_matches = list(_YEAR_RE.finditer(region))
    if not year_matches:
        # No recognisable year label at all. Mirror _extract_max_dpd's own
        # digit-density heuristic: a genuinely blank grid (brand new
        # account, no history yet) has almost no digits in this region, so
        # that's a confident (0, 0, 0) - not "unreadable". A digit-dense
        # region with no year label we could parse is genuinely garbled,
        # and stays (None, None, None).
        if len(re.findall(r'\d', region)) >= 6:
            return None, None, None
        return 0, 0, 0

    calendar_positions = 0
    twelve_month_boundary = None  # index into chronological where 12 months is reached
    chronological = []  # most-recent-first, real (non-blank) readings only, UNCAPPED
    seen_years = set()
    for i, ym in enumerate(year_matches):
        year = ym.group(1)
        if year in seen_years:
            break
        seen_years.add(year)
        cell_start = ym.end()
        cell_end = year_matches[i + 1].start() if i + 1 < len(year_matches) else len(region)
        cells = _CELL_RE.findall(region[cell_start:cell_end])[:12]
        for token in reversed(cells):
            if token.strip() == '-':
                continue
            calendar_positions += 1
            dpd = _cell_to_dpd(token)
            if dpd is not None:
                chronological.append(dpd)
            if calendar_positions == 12 and twelve_month_boundary is None:
                twelve_month_boundary = len(chronological)

    if not chronological:
        return None, None, None
    twelve_mo_values = chronological[:twelve_month_boundary] if twelve_month_boundary is not None else chronological
    max_12mo = max(twelve_mo_values) if twelve_mo_values else 0
    max_alltime = max(chronological)
    return chronological[0], max_12mo, max_alltime


def _has_written_off_signal(block: str) -> bool:
    """
    Shared by _is_closed (rules 2 & 4) and _is_written_off - a non-zero
    write-off amount field. Remarks text alone ("Written-off") is NOT
    sufficient: CRIF prints that word on still-open, still-reporting
    accounts too (a historical/partial write-off note, or bleed-through
    from a co-obligant's own record) while Total/Principal Writeoff Amt
    stays 0 and the account keeps getting monthly DPD updates - confirmed
    on a real report where 5 genuinely active accounts (zero balance,
    zero write-off amount, live DPD grid) were wrongly flipped to Closed
    by remarks text alone. Factored out so the two regex pairs can't drift
    apart from each other the way the UI badge's overdue tolerance once
    drifted from validate_extraction()'s.
    """
    wo_m = re.search(
        r'(?:Total\s+)?Write\s*[- ]?[Oo]ff\s+Amt[:\s]*\n?\s*([\d,]+)',
        block, re.IGNORECASE,
    )
    return bool(wo_m and to_int(wo_m.group(1)) != 0)


def _is_closed(block: str) -> bool:
    """
    Rule 1: Closed Date has a valid date.
    Rule 2: Total/write-off amount field is non-zero (see
            _has_written_off_signal - remarks text alone is deliberately
            NOT trusted here).
    Rule 3: Compact block  -  'Closed' before any field label.
    """
    val = _next_line_value(block, "Closed Date:")
    if val and re.match(r'\d{2}-\d{2}-\d{4}', val):
        return True
    m = re.search(r'Closed\s+Date\s*:\s*(\S+)', block)
    if m and re.match(r'\d{2}-\d{2}-\d{4}', m.group(1)):
        return True

    if _has_written_off_signal(block):
        return True

    first_field = re.search(
        r'(?:Ownership|Disbursed Date|Current Balance|Closed Date|Account Type)\s*:',
        block,
    )
    header_region = block[: first_field.start()] if first_field else block[:300]
    if re.search(r'\nClosed\n', header_region):
        return True

    return False


def _is_written_off(block: str) -> bool:
    """
    Narrower than _is_closed: CRIF Retail has no separate "Written Off"
    status (unlike Commercial) - a written-off account still just shows
    "Closed" here. This flags the write-off signal specifically so the
    Credit Analysis rollup can report it as its own bucket rather than
    lumping it into generic Closed.
    """
    return _has_written_off_signal(block)


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


def build_positional_dpd(text: str) -> list:
    """
    Max DPD per 'Account Type:' label, scanning the text between one label and
    the next (not the account block). Some CRIF layouts print the compact
    summary's Payment History grid for ALL accounts before the detailed blocks,
    with the 'Payment History/Asset Classification:' label trailing the grid
    instead of preceding it  -  in that layout _extract_max_dpd's block-relative
    "scan after the label" search throws the grid away entirely. Anchoring on
    the 'Account Type:' labels (same anchors as build_positional_lists) instead
    isolates each account's own grid regardless of where the label falls.
    """
    labels = list(re.finditer(r'Account Type:', text))
    result = []
    for i, m in enumerate(labels):
        start   = m.end()
        end     = labels[i + 1].start() if i + 1 < len(labels) else len(text)
        segment = text[start:end]
        vals    = [
            int(num)
            for num, cls in re.findall(r'(?<!\d)(\d{1,3})\s*/\s*([A-Za-z]{2,3})', segment)
            if cls.upper() not in _DPD_FREQ and int(num) < 900
        ]
        if vals:
            result.append(max(vals))
        else:
            letter_dpd = _letter_placeholder_dpd(segment)
            result.append(letter_dpd if letter_dpd is not None else 0)
    return result


# ─────────────────────────────────────────────────────────────────
# ACCOUNT EXTRACTION
# ─────────────────────────────────────────────────────────────────

_ACCT_NUM_RE = re.compile(r'Account\s*#:\s*([A-Za-z0-9]+)', re.IGNORECASE)


def _distinct_real_account_numbers(block: str) -> set:
    """
    Real (non-masked) 'Account #:' values found anywhere in this block.
    Masked placeholders ('xxxx'/'XXXX') are excluded - CRIF masks them
    identically across genuinely different accounts, so a repeated mask
    proves nothing either way (see extract_account's own KNOWN GAP note).
    A real alphanumeric code is unmasked and unique per account; finding
    two DIFFERENT real codes in one block is unambiguous proof this
    block's captured text span swallowed more than one account's own
    header+grid - confirmed on a real report where one account's block
    literally contained three other accounts' full 'Account Type: ...
    Account #: <code> ... As on: <date>' sections and grids, a page/column
    reading-order fault in the underlying PDF text extraction, not merely
    the milder single-value "sibling DPD" leak this function otherwise
    can't rule out.
    """
    return {
        m.group(1) for m in _ACCT_NUM_RE.finditer(block)
        if not re.fullmatch(r'x+', m.group(1), re.IGNORECASE)
    }


def _dpd_span_contaminated(block: str) -> bool:
    return len(_distinct_real_account_numbers(block)) > 1


def extract_account(acct_num: int, block: str,
                    loan_type: str = None, entity: str = None,
                    max_dpd: int = None) -> dict:
    block_dpd = _extract_max_dpd(block)
    last_reported_dpd, max_dpd_12mo, grid_max_alltime = _extract_dpd_window(block)
    # Positional dpd (from the compact summary grid), the block's own flat
    # scan, and the grid-window walk's own all-time max (grid_max_alltime -
    # preferred over block_dpd when available, since it doesn't share
    # block_dpd's blind spot: block_dpd only falls back to a letter-
    # placeholder reading when NO numeric cell exists anywhere in the block,
    # so it can silently miss a worse 'XXX/SUB'-style reading sitting
    # alongside lower numeric ones - confirmed on a real report, see
    # _extract_dpd_window's own docstring) - can each be a genuine int or an
    # unreadable None - take the higher of whichever are readable.
    #
    # KNOWN GAP (investigated, not fixed): on page-dense reports where one
    # block's captured span holds more than one "As on:" grid section, this
    # max() can let a sibling account's higher DPD silently win over the
    # correct positional read (confirmed on a real report - 5 accounts with
    # measurably wrong max_dpd, e.g. 556 vs the true 182). No reliable way
    # was found to detect this from the text alone: the report masks Account
    # #/Credit Grantor identically ("xxxx") across both a genuine same-
    # account re-report (multiple "As on:" snapshots of ONE account's own
    # evolving history - where trusting the higher block-scanned value is
    # correct and intentional) and true sibling contamination (a different
    # account's grid bled in) - and both shapes occur in real reports,
    # including ones otherwise verified fully correct. Flagging here rather
    # than shipping an untested heuristic that could silently break the
    # legitimate case.
    _dpd_candidates = [d for d in (max_dpd, block_dpd, grid_max_alltime) if d is not None]
    combined_dpd = max(_dpd_candidates) if _dpd_candidates else None
    # Distinct from the KNOWN GAP above: when the block demonstrably holds more
    # than one account's own header+grid (proven by two different real, unmasked
    # 'Account #:' codes - not just the ambiguous same-masked-value case), none
    # of max_dpd/last_reported_dpd/max_dpd_12mo can be trusted as THIS account's
    # own reading, since any of the three candidate sources above may have
    # silently picked up a different account's grid entirely. Report unreadable
    # rather than a plausible-looking but potentially wrong number.
    dpd_block_contaminated = _dpd_span_contaminated(block)
    if dpd_block_contaminated:
        combined_dpd = last_reported_dpd = max_dpd_12mo = None
    return {
        "sr_no":            acct_num,
        # Internal signal for parser.py's Vision-fallback gate, not shown in
        # the UI/Excel - distinguishes "unreadable because the block's text
        # span provably swallowed another account's data" (worth spending a
        # Vision call to recover, even on a digital report) from an ordinary
        # blank/garbled grid (recovering that on a digital report would just
        # re-read the same blank cells Vision has no more insight into).
        "dpd_block_contaminated": dpd_block_contaminated,
        "date_of_sanction": _extract_date(block),
        "sanction_amount":  _extract_sanction_amt(block),
        "current_balance":  _extract_balance(block),
        "emi":              _extract_emi(block),
        "overdue":          _extract_overdue(block),
        "entity":           entity if entity else _extract_entity(block),
        "ownership":        _extract_ownership(block),
        "type_of_loan":     loan_type if loan_type else _extract_loan_type(block),
        "max_dpd":          combined_dpd,
        "last_reported_dpd": last_reported_dpd,
        "max_dpd_12mo":     max_dpd_12mo,
        "status":           "Closed" if _is_closed(block) else "Active",
        "written_off":      _is_written_off(block),
    }


# ─────────────────────────────────────────────────────────────────
# PORTFOLIO-LEVEL ANALYSIS  (Credit Analysis sheet / UI section)
# ─────────────────────────────────────────────────────────────────
# CRIF Retail's own report has no market-comparison table like Commercial's
# Borrower Summary (no "your institution vs other institutions" section
# exists in this report format at all) - so unlike Commercial's analysis,
# there's no such section here. What IS derivable, the same way Commercial's
# credit_profile_summary/derog_summary are (from the already-extracted
# accounts, not re-parsed from a report table that might be truncated):
# a loan-type distribution and a written-off/overdue rollup.

def credit_profile_summary(accounts: list) -> list:
    """
    Loan-type distribution of Active accounts (Personal Loan, Housing Loan,
    Credit Card, Gold Loan, ...). Returns a list of {asset_class, count,
    outstanding} sorted by outstanding balance descending - same shape as
    Commercial's asset-class distribution (asset_class here holds the loan
    type name, not a DPD bucket) so it renders through the same UI/Excel
    code with just a different section label.
    """
    buckets = {}
    for a in accounts:
        if a.get("status") != "Active":
            continue
        lt = a.get("type_of_loan") or "Unknown"
        b = buckets.setdefault(lt, {"count": 0, "outstanding": 0})
        b["count"] += 1
        b["outstanding"] += a.get("current_balance") or 0
    return sorted(
        [{"asset_class": lt, **v} for lt, v in buckets.items()],
        key=lambda r: r["outstanding"], reverse=True,
    )


def derog_summary(accounts: list) -> dict:
    """
    Rollup of red-flag accounts across all extracted accounts - count and
    total amount per category. Narrower than Commercial's version: CRIF
    Retail has no Settled/Suit Filed/Delinquent concepts in this report
    format, only a write-off signal (folded into "Closed" status - see
    _is_written_off) and overdue amounts on still-open accounts.

    Written Off uses sanction_amount, not current_balance: the bureau zeroes
    current_balance once an account is written off, so summing it would show
    a misleading "Rs.0 impact" for accounts that may carry a large original
    exposure. Overdue uses the overdue field directly - it's already the
    "how much is currently past due" figure for a live account.
    """
    written_off = [a for a in accounts if a.get("written_off")]
    overdue     = [a for a in accounts
                   if a.get("status") == "Active" and (a.get("overdue") or 0) > 0]
    return {
        "written_off": {"count": len(written_off),
                         "amount": sum(a.get("sanction_amount") or 0 for a in written_off)},
        "overdue":     {"count": len(overdue),
                         "amount": sum(a.get("overdue") or 0 for a in overdue)},
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

    dpd_list = build_positional_dpd(text)
    dpd_ok   = len(dpd_list) == len(blocks)

    accounts = []
    for idx, (num, blk) in enumerate(blocks):
        lt = at_list[idx] if at_ok else None
        en = cg_list[idx] if cg_ok else None
        if not lt or lt == "Unknown":
            lt = _extract_loan_type(blk)
        if not en or en == "NA":
            en = _extract_entity(blk)
        dp = dpd_list[idx] if dpd_ok else None
        accounts.append(extract_account(num, blk, lt, en, dp))

    accounts.sort(key=lambda x: x["sr_no"])
    return name, score, blocks, accounts, reported
