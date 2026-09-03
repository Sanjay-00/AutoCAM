import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crif_commercial_parser import parse_crif_commercial, _extract_dpd_window


def _parse_one_account(payment_history_grid: str):
    block = (
        "Type:\n"
        "Commercial Vehicle Loan -In INR\n"
        "Account #: 12345\nAmount Overdue: 0\nSanctioned Amount: 10,00,000\n"
        "DPD/Asset\nClassification:\nSTANDARD\n"
        "Current Balance: 0\nCurrent Balance History (12 Months):\n"
        "Current Balance amounts in Lakhs\n"
        "Payment History/Asset Classification:\n" + payment_history_grid
    )
    text = "Commercial ACE Report\nBorrower Summary\nLoan Terms For:\n" + block
    name, score, blocks, accounts, reported_totals, analysis = parse_crif_commercial(text)
    assert accounts, "expected at least one parsed account"
    return accounts[0]


def test_max_dpd_12mo_is_independently_windowed_not_an_alias_for_max_dpd():
    # The 'Current Balance History (12 Months)' table's label does NOT mean
    # the separate Payment History/DPD grid below it is also 12-month-
    # scoped - confirmed on a real report where the true trailing-12-month
    # max (47) differed from the all-time max (78, from 19 months earlier).
    # This grid mirrors that shape: a high DPD sits outside the most recent
    # 12 calendar months.
    grid = (
        "January\nFebruary\nMarch\nApril\nMay\nJune\nJuly\nAugust\n"
        "September\nOctober\nNovember\nDecember\n"
        "2026\n017/xxx\n014/xxx\n017/xxx\n016/xxx\n017/xxx\n016/xxx\n017/xxx\n034/xxx\n-\n-\n-\n-\n"
        "2025\n078/xxx\n045/xxx\n017/xxx\n016/xxx\n017/xxx\n016/xxx\n017/xxx\n017/xxx\n047/xxx\n047/xxx\n047/xxx\n017/xxx\n"
    )
    a = _parse_one_account(grid)
    assert a["max_dpd"] == 78          # all-time max, now computed from this same grid walk
    assert a["max_dpd_12mo"] == 47     # trailing 12 months only (Sep-25..Aug-26)
    assert a["last_reported_dpd"] == 34


def test_floor_rejected_small_values_dont_silently_widen_the_window():
    # A real report had several genuine (digital, non-OCR) small readings
    # ('001/xxx') that _commercial_cell_to_dpd's noise floor rejects as
    # presumed OCR noise - an earlier version of this window-walk counted
    # only floor-PASSING values toward "have we covered 12 months yet",
    # so it kept reaching into a 13th calendar month to compensate,
    # silently pulling in a stale high reading (043) that sat just outside
    # the true 12-month window. The window boundary must be driven by
    # calendar months actually walked, not by how many values pass the
    # floor.
    grid = (
        "January\nFebruary\nMarch\nApril\nMay\nJune\nJuly\nAugust\n"
        "September\nOctober\nNovember\nDecember\n"
        "2026\n001/xxx\n001/xxx\n001/xxx\n011/xxx\n012/xxx\n011/xxx\n012/xxx\n-\n-\n-\n-\n-\n"
        "2025\n043/xxx\n009/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n001/xxx\n"
    )
    a = _parse_one_account(grid)
    assert a["max_dpd_12mo"] == 12   # the 043 from Jan-2025 is the 13th month back, must not appear
    assert a["last_reported_dpd"] == 12
    assert a["max_dpd"] == 43        # all-time DOES include the 13th-month 043


def test_max_dpd_12mo_defaults_to_confident_zero_when_all_real_cells_are_floor_noise():
    # Mirrors _extract_max_dpd's own convention: real (non-blank) cells were
    # found, but none passed the noise floor -> confident 0, not None
    # ("unreadable").
    grid = "January\nFebruary\nMarch\nApril\nMay\nJune\nJuly\nAugust\nSeptember\nOctober\nNovember\nDecember\n2026\n-\n-\n-\nxxx/xxx\nxxx/xxx\n000/xxx\n000/xxx\n003/xxx\n-\n-\n-\n-\n"
    a = _parse_one_account(grid)
    assert a["max_dpd_12mo"] == 0
    assert a["max_dpd"] == 0
    assert a["last_reported_dpd"] == 3   # last_reported is UNFILTERED (raw), unlike the floored fields


def test_named_nonstandard_bucket_trusted_at_any_value_no_floor():
    grid = "January\nFebruary\nMarch\nApril\nMay\nJune\nJuly\nAugust\nSeptember\nOctober\nNovember\nDecember\n2026\n002/SMA\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n-\n"
    a = _parse_one_account(grid)
    assert a["max_dpd_12mo"] == 2
    assert a["last_reported_dpd"] == 2


def test_ocr_garbled_class_is_still_recognized_unlike_the_old_strict_scan():
    # Confirmed on a real scanned report: OCR mangled '012/xxx' into
    # 'O12 /rox' (letter O for zero, 'rox' for 'xxx'). _extract_max_dpd's
    # strict class allowlist (SMA/SUB/DBT/LOS/xxx/STD only) doesn't match
    # 'rox' at all and silently fell back to a less precise reading (0) for
    # an account with a real DPD of 12. This grid walk is anchored to the
    # actual Payment History region (via the year label), so it can safely
    # accept any letter class without the false-positive risk a whole-block
    # scan would have.
    grid = "January\nFebruary\nMarch\nApril\nMay\nJune\nJuly\nAugust\nSeptember\nOctober\nNovember\nDecember\n2026\n-\n-\n-\n-\nO12 /rox\n-\n-\n-\n-\n-\n-\n-\n"
    a = _parse_one_account(grid)
    assert a["max_dpd"] == 12
    assert a["max_dpd_12mo"] == 12
    assert a["last_reported_dpd"] == 12


def test_no_year_label_and_sparse_digits_reads_as_confident_blank():
    last_reported, max_12mo, max_alltime = _extract_dpd_window("no grid data here at all")
    assert (last_reported, max_12mo, max_alltime) == (0, 0, 0)


def test_no_year_label_but_digit_dense_reads_as_unreadable():
    garbled = "08K 019K 902K 345K 671K 890K"
    last_reported, max_12mo, max_alltime = _extract_dpd_window(garbled)
    assert (last_reported, max_12mo, max_alltime) == (None, None, None)
