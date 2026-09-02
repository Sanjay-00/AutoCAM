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
    assert _extract_dpd_window(block) == (0, 0)

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
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 875
    assert max_12mo == 875

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
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 30
    assert max_12mo == 95

def test_letter_placeholder_cells_map_to_representative_dpd():
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\nXXX/LOS\nXXX/STD\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 0    # Feb = XXX/STD -> 0, most recent populated
    assert max_12mo == 181       # Jan = XXX/LOS -> 181, worst in window

def test_unreadable_grid_returns_none_none():
    # Digit-dense (garbled OCR noise) but no parseable "NNN/class" cell or
    # year label - genuinely unreadable, not a blank/new-account grid.
    # (Finding 3: the original version of this block had zero digits at
    # all, which under the digit-density heuristic now shared with
    # _extract_max_dpd actually reads as a confident blank (0, 0), not
    # "unreadable" - updated to a properly digit-dense garbled sample so
    # this test still exercises the None/None path it's named for.)
    block = "Payment History/Asset Classification:\n038K 019K 902K 011K 384K 720K\n"
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported is None
    assert max_12mo is None

def test_blank_grid_with_no_year_label_reads_as_confident_zero():
    # No year-block label at all, but also no real grid content - a
    # genuinely blank/new-account grid, same case _extract_max_dpd already
    # treats as a confident 0 via its digit-density heuristic (Finding 3).
    block = (
        "Payment History/Asset Classification:\n"
        "No history reported for this account.\n"
    )
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 0
    assert max_12mo == 0


def test_garbled_grid_with_no_year_label_still_returns_none_none():
    # No recognisable year label, but the region is digit-dense (garbled
    # grid, not a blank one) - stays unreadable, unlike the blank case above.
    block = (
        "Payment History/Asset Classification:\n"
        "038K 019K 902K 011K 384K 720K 293K 048K\n"
    )
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported is None
    assert max_12mo is None


def test_as_on_date_year_does_not_corrupt_the_scan():
    # "As on: 31-03-2026" carries a "2026" that must NOT be mistaken for a
    # grid year-block label - if it is, it gets consumed as a bogus
    # zero-cell block, and then the real "2026\n" label hits the
    # duplicate-year guard and breaks the scan early (Finding 4).
    # Single populated cell so the assertion doesn't depend on within-year
    # cell-ordering convention - only on whether the "As on" date's year
    # gets misread as a bogus grid-year-block label.
    block = (
        "Payment History/Asset Classification:\n"
        "As on: 31-03-2026\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n-\n-\n045/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 45
    assert max_12mo == 45


def test_corrupted_block_stops_at_repeated_year_label():
    # A merged/corrupted block repeats the whole grid section - the second
    # occurrence of "2026" marks a stale duplicate and must be ignored.
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n010/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
        "Account Type: COMMERCIAL VEHICLE LOAN\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n999/XXX\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 10
    assert max_12mo == 10
