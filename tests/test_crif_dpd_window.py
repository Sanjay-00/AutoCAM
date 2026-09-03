import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crif_parser import _extract_dpd_window, _extract_max_dpd


def test_last_account_blank_grid_not_corrupted_by_trailing_report_text():
    # The report's LAST account has no next "Account Information" header to
    # bound its block, so a genuinely blank Payment History section can run
    # on into the document-level Inquiries section, "-END OF REPORT-", and
    # the Appendix/glossary - all digit-heavy, which used to fool the
    # digit-density "blank vs. garbled" heuristic into reporting a
    # confidently-blank account as unreadable (confirmed on a real report).
    block = (
        "Account Information\n47\n  Ownership:\nINDIVIDUAL\n"
        "Current Balance:\n0\nClosed Date:\n29-01-2023\n"
        "Payment History/Asset Classification:\n"
        "Inquiries ( past 24 months)\nCredit Grantor\nType\nDate of Inquiry\n"
        "Account Type\nAmount\nXXXX\nNBF\n16-07-2026\nConsumer Loan\n15,000\n"
        "XXXX\nHFC\n02-04-2026\nOTHER\n3,00,000\n"
        "-END OF REPORT-\nAppendix\nSection\nCode\nDescription\n"
        "8/17/26, 5:43 PM\n"
    )
    assert _extract_max_dpd(block) == 0
    assert _extract_dpd_window(block) == (0, 0, 0)

def test_single_year_partial_grid_last_reported_is_rightmost_populated():
    # 875 (not the disputed 900 boundary - see Finding 7 of the whole-branch
    # review, which aligned this reader's ceiling to _extract_max_dpd's own
    # >= 900 "reject as OCR noise" floor) still clearly demonstrates
    # "rightmost populated cell wins" without touching that threshold.
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n875/XXX\n875/XXX\n875/XXX\n875/XXX\n875/XXX\n875/XXX\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert last_reported == 875
    assert max_12mo == 875
    assert max_alltime == 875

def test_window_spans_year_boundary():
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n010/XXX\n020/XXX\n030/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
        "2025\n000/STD\n000/STD\n000/STD\n000/STD\n000/STD\n000/STD\n"
        "095/SUB\n000/STD\n000/STD\n000/STD\n000/STD\n000/STD\n"
    )
    # 2026 has 3 populated months (Jan-Mar: 10,20,30, most-recent-first -> 30,20,10)
    # need 9 more from 2025 read Dec->Jan: 0,0,0,0,0,0,95,0,0 (9 values) -> total 12
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert last_reported == 30
    assert max_12mo == 95
    assert max_alltime == 95  # nothing beyond the 12-month window is higher here

def test_letter_placeholder_cells_map_to_representative_dpd():
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\nXXX/LOS\nXXX/STD\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert last_reported == 0    # Feb = XXX/STD -> 0, most recent populated
    assert max_12mo == 181       # Jan = XXX/LOS -> 181, worst in window
    assert max_alltime == 181

def test_unreadable_grid_returns_none_none():
    # Digit-dense (garbled OCR noise) but no parseable "NNN/class" cell or
    # year label - genuinely unreadable, not a blank/new-account grid.
    block = "Payment History/Asset Classification:\n038K 019K 902K 011K 384K 720K\n"
    assert _extract_dpd_window(block) == (None, None, None)

def test_blank_grid_with_no_year_label_reads_as_confident_zero():
    # No year-block label at all, but also no real grid content - a
    # genuinely blank/new-account grid, same case _extract_max_dpd already
    # treats as a confident 0 via its digit-density heuristic.
    block = (
        "Payment History/Asset Classification:\n"
        "No history reported for this account.\n"
    )
    assert _extract_dpd_window(block) == (0, 0, 0)


def test_garbled_grid_with_no_year_label_still_returns_none_none():
    # No recognisable year label, but the region is digit-dense (garbled
    # grid, not a blank one) - stays unreadable, unlike the blank case above.
    block = (
        "Payment History/Asset Classification:\n"
        "038K 019K 902K 011K 384K 720K 293K 048K\n"
    )
    assert _extract_dpd_window(block) == (None, None, None)


def test_as_on_date_year_does_not_corrupt_the_scan():
    # "As on: 31-03-2026" carries a "2026" that must NOT be mistaken for a
    # grid year-block label - if it is, it gets consumed as a bogus
    # zero-cell block, and then the real "2026\n" label hits the
    # duplicate-year guard and breaks the scan early.
    block = (
        "Payment History/Asset Classification:\n"
        "As on: 31-03-2026\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n-\n-\n045/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert last_reported == 45
    assert max_12mo == 45
    assert max_alltime == 45


def test_corrupted_block_stops_at_repeated_year_label():
    # A merged/corrupted block repeats the whole grid section - the second
    # occurrence of "2026" marks a stale duplicate and must be ignored, even
    # under the now-uncapped all-time walk.
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n010/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
        "Account Type: COMMERCIAL VEHICLE LOAN\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n999/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert last_reported == 10
    assert max_12mo == 10
    assert max_alltime == 10  # the duplicate's 999 must never appear


def test_unresolvable_but_present_cell_does_not_widen_the_window():
    # A real report had 'XXX/XXX' cells (present, but not a recognised
    # classification) sitting between real readings - these must still
    # count as a calendar month consumed, or the window silently reaches
    # into a 13th+ month to compensate (the same bug class already found
    # and fixed on the CRIF Commercial ACE side). Here, 11 unresolvable
    # 'XXX/XXX' cells plus 1 real reading exactly fill the 12-month window,
    # so a 13th-month value (290, comfortably under the >=900 OCR-noise
    # ceiling already established elsewhere) sitting just outside it must
    # not appear in max_12mo, even though it's within the all-time max.
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n020/XXX\nXXX/XXX\nXXX/XXX\nXXX/XXX\nXXX/XXX\nXXX/XXX\n"
        "XXX/XXX\nXXX/XXX\nXXX/XXX\nXXX/XXX\nXXX/XXX\nXXX/XXX\n"
        "2025\n290/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert last_reported == 20
    assert max_12mo == 20     # the 290 from 2025 must NOT appear here
    assert max_alltime == 290  # but it's correctly captured all-time


def test_max_dpd_alltime_recovers_letter_placeholder_missed_by_extract_max_dpd():
    # Confirmed on a real report: _extract_max_dpd only falls back to a
    # letter-placeholder reading when NO numeric cell exists anywhere in the
    # block - so a block with both a real numeric reading (27) and a worse
    # letter-placeholder reading (XXX/SUB -> 91) silently reports only 27.
    # max_dpd_alltime must recover the true worst reading (91).
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n027/STD\nXXX/SUB\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    assert _extract_max_dpd(block) == 27  # the pre-existing blind spot
    last_reported, max_12mo, max_alltime = _extract_dpd_window(block)
    assert max_alltime == 91
