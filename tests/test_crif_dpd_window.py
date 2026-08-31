import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crif_parser import _extract_dpd_window

def test_single_year_partial_grid_last_reported_is_rightmost_populated():
    block = (
        "Payment History/Asset Classification:\n"
        "Jan\nFeb\nMar\nApr\nMay\nJun\nJul\nAug\nSep\nOct\nNov\nDec\n"
        "2026\n900/XXX\n900/XXX\n900/XXX\n900/XXX\n900/XXX\n900/XXX\n-\n-\n-\n-\n-\n-\n"
    )
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported == 900
    assert max_12mo == 900

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
    block = "Payment History/Asset Classification:\nNo grid data of any kind here.\n"
    last_reported, max_12mo = _extract_dpd_window(block)
    assert last_reported is None
    assert max_12mo is None

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
