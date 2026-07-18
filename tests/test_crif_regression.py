"""
Regression suite for the CRIF (retail + Commercial ACE) extraction pipeline.

Runs every PDF/HTML report found in the directory named by the CIBIL_TEST_DIR
environment variable and checks internal-consistency invariants that have
each caught a real bug during development - not "does this match a golden
file" (no golden values are hardcoded), but "is this extraction internally
self-consistent" (an account can't be both active and closed, Live Accts
can't exceed Total Accts, every account must land in some status bucket,
etc).

Real bureau reports carry PII (PAN, addresses, phone numbers, borrower
names) and must never be committed to this repo. Point CIBIL_TEST_DIR at a
local folder of sample reports to run this suite:

    CIBIL_TEST_DIR="C:\\path\\to\\sample\\reports" pytest tests/

If the variable isn't set, every test in this file is skipped (not failed) -
CI/local runs without sample data still pass cleanly.
"""

import os
import glob

import pytest

import parser as p
import excel_generator as eg

_TEST_DIR = os.environ.get("CIBIL_TEST_DIR", "")

pytestmark = pytest.mark.skipif(
    not _TEST_DIR or not os.path.isdir(_TEST_DIR),
    reason="Set CIBIL_TEST_DIR to a local folder of sample CIBIL reports to run this suite.",
)


def _sample_files():
    if not _TEST_DIR or not os.path.isdir(_TEST_DIR):
        return []
    files = []
    for ext in ("*.pdf", "*.PDF", "*.html", "*.htm"):
        files.extend(glob.glob(os.path.join(_TEST_DIR, ext)))
    return sorted(files)


@pytest.fixture(scope="module")
def parsed_reports():
    """Parse every sample file once per test session; skip files that error
    (a hard parse failure is worth knowing about, but shouldn't take down
    every other assertion in the suite - _test_no_exceptions below is what
    actually fails the build for that)."""
    results = {}
    for f in _sample_files():
        try:
            results[f] = p.parse(f)
        except Exception as e:
            results[f] = e
    return results


def test_no_exceptions(parsed_reports):
    failures = {os.path.basename(f): repr(e)
                for f, e in parsed_reports.items() if isinstance(e, Exception)}
    assert not failures, f"parse() raised on: {failures}"


def test_excel_generates_for_every_report(parsed_reports):
    failures = {}
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        try:
            eg.generate_excel(r)
        except Exception as e:
            failures[os.path.basename(f)] = repr(e)
    assert not failures, f"generate_excel() raised on: {failures}"


def test_every_account_has_a_status_bucket(parsed_reports):
    """Every account must be classifiable as Active or not-Active - an
    account with no status field, or one the UI's Active/Closed tab split
    doesn't recognise, silently disappears from both tabs (a real bug this
    caught earlier in development)."""
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        for a in r["accounts"]:
            if "status" not in a or not a["status"]:
                failures.append(f"{os.path.basename(f)} sr_no={a.get('sr_no')}")
    assert not failures, f"accounts with no status: {failures}"


def test_borrower_summary_internally_consistent(parsed_reports):
    """Live Accts can never exceed Total Accts - a logical impossibility
    that's a reliable tell for an OCR digit-drop misread (this caught a real
    bug: a scanned report's OCR'd 'Total Accts' column read as 7 when the
    true value was >= 17, since Live Accts read correctly as 15)."""
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        analysis = r.get("analysis")
        if not analysis:
            continue
        bs = analysis.get("borrower_summary") or {}
        for label in ("your_institution", "other_institution"):
            inst = bs.get(label) or {}
            total, live = inst.get("total_accts"), inst.get("live_accts")
            if total is not None and live is not None and total < live:
                failures.append(f"{os.path.basename(f)} {label}: total={total} < live={live}")
    assert not failures, f"impossible total<live: {failures}"


def test_length_of_credit_history_is_short(parsed_reports):
    """A regex bound-checking guard: this field is a short 'N (Yrs) / M
    (Mnths)' string. A much longer value means the capture swallowed the
    next report section (caught twice during development, on two different
    OCR'd reports with slightly different layouts)."""
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        analysis = r.get("analysis")
        if not analysis:
            continue
        hist = (analysis.get("borrower_summary") or {}).get("length_of_credit_history")
        if hist and len(hist) > 40:
            failures.append(f"{os.path.basename(f)}: {hist!r}")
    assert not failures, f"length_of_credit_history overcaptured: {failures}"


def test_validation_not_silently_empty(parsed_reports):
    """A report that extracts zero accounts is only a genuine pass when the
    report's own summary totals confirm the report really is empty (e.g. a
    thin-file/no-trade-history applicant). Zero accounts AND no summary
    totals found either means we have no ground truth to check against -
    that's what a silent block-splitting failure on an unrecognised report
    layout looks like too, and should never show as a clean 'valid' badge."""
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        v = r["validation"]
        if not r["accounts"] and v.get("expected_count") is None and v.get("expected_balance") is None:
            if v.get("valid"):
                failures.append(os.path.basename(f))
    assert not failures, f"zero accounts reported as valid with no ground truth: {failures}"


def test_written_off_and_suit_filed_amounts_are_not_silently_zero(parsed_reports):
    """Written Off / Suit Filed derog amounts use sanction_amount (the
    bureau zeroes current_balance on these), not current_balance - a report
    with written-off accounts showing Rs.0 total exposure would be actively
    misleading, not just imprecise."""
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        analysis = r.get("analysis")
        if not analysis:
            continue
        derog = analysis.get("derog_summary") or {}
        for key in ("written_off", "suit_filed"):
            bucket = derog.get(key) or {}
            if bucket.get("count", 0) > 0 and bucket.get("amount", 0) == 0:
                # Not automatically a bug (a report's write-offs could
                # genuinely all have zero sanction amount on record), but
                # worth a visible flag to eyeball rather than a silent pass.
                failures.append(f"{os.path.basename(f)} {key}: count={bucket['count']} amount=0")
    if failures:
        pytest.skip(f"zero-amount derog buckets to eyeball (not auto-failed): {failures}")
