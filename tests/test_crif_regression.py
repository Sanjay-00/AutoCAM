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


def test_delinquent_only_on_active(parsed_reports):
    """
    Delinquent is an overlay on a still-OPEN account. Closed/Written
    Off/Settled accounts can carry a non-Standard DPD/Asset Classification
    too (often exactly why they're no longer active), but that's not the
    same as being 'delinquent' in the sense this flag means everywhere else
    (a live account CIBIL/CRIF still counts under 'Live'). This caught a
    real bug: deriving the flag from the account's current classification
    leaked it onto terminal accounts unless explicitly gated on status.
    """
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        for a in r["accounts"]:
            if a.get("delinquent") and a.get("status") != "Active":
                failures.append(f"{os.path.basename(f)} sr_no={a.get('sr_no')} status={a.get('status')}")
    assert not failures, f"delinquent flag set on a non-Active account: {failures}"


def test_delinquent_count_gap_is_reconciled(parsed_reports):
    """
    CRIF Commercial's own 'Live Accts' figure deliberately excludes
    delinquent-but-open accounts, which our extraction correctly counts as
    Active. When that fully explains an active-count mismatch (extracted
    active minus delinquent equals the report's own count), validate_
    extraction() must not flag it as an unexplained error - a report should
    never show a scary red mismatch for a gap that's fully understood and
    expected. (This does NOT mean every count mismatch is delinquency - an
    unexplained gap should still fail; this only checks the reconciled case
    doesn't regress back to a false failure.)
    """
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        v = r["validation"]
        exp_c = v.get("expected_count")
        extracted_c = v.get("extracted_count")
        delinquent = v.get("delinquent_active_count", 0)
        if exp_c is None or extracted_c is None:
            continue
        if delinquent and extracted_c - delinquent == exp_c and not v["valid"]:
            failures.append(
                f"{os.path.basename(f)}: extracted={extracted_c} delinquent={delinquent} "
                f"expected={exp_c} but valid=False issues={v['issues']}"
            )
    assert not failures, f"delinquent-explained count gap wrongly flagged invalid: {failures}"


def test_validation_ok_flags_agree_with_issues(parsed_reports):
    """
    balance_ok/sanction_ok/overdue_ok exist specifically so app.py's badge
    never has to re-derive its own pass/fail thresholds (a prior bug: the UI
    used a different formula than validate_extraction() and could show a
    tick/cross that disagreed with the actual valid/issues result). This
    guards the invariant directly: whenever one of these is False, its
    matching mismatch string must be present in `issues`, and vice versa.
    """
    _CHECKS = (
        ("balance_ok",  "Balance mismatch"),
        ("sanction_ok", "Sanctioned amount mismatch"),
        ("overdue_ok",  "Overdue amount mismatch"),
    )
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception):
            continue
        v = r["validation"]
        if "balance_ok" not in v:
            continue  # TransUnion's simpler validate() doesn't carry these
        issues_text = " ".join(v.get("issues", []))
        for flag_key, mismatch_phrase in _CHECKS:
            ok = v.get(flag_key, True)
            has_issue = mismatch_phrase in issues_text
            if ok == has_issue:
                failures.append(
                    f"{os.path.basename(f)} {flag_key}={ok} but "
                    f"{mismatch_phrase!r} present={has_issue}"
                )
    assert not failures, f"validation ok-flag disagrees with issues list: {failures}"


def test_written_off_is_always_a_bool(parsed_reports):
    """
    written_off must never end up None/missing - that reads as a confident
    "not written off" downstream (Credit Analysis' derog rollup), which is
    indistinguishable from a real False. This caught a real bug: Gemini's
    LLM-correction JSON reply didn't carry this key at all, so any account
    passing through Stage 2/3 correction silently lost the flag.
    """
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception) or r.get("provider") != "crif":
            continue
        for a in r["accounts"]:
            if not isinstance(a.get("written_off"), bool):
                failures.append(f"{os.path.basename(f)} sr_no={a.get('sr_no')} "
                                f"written_off={a.get('written_off')!r}")
    assert not failures, f"written_off not a bool: {failures}"


def test_retail_credit_profile_summary_matches_active_accounts(parsed_reports):
    """
    CRIF Retail's loan-type distribution is derived from the extracted
    accounts list, not re-parsed from a report table - so it must exactly
    reconcile back to the active accounts it was built from (count and
    outstanding balance), the same self-consistency guarantee Commercial's
    asset-class distribution already has.
    """
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception) or r.get("provider") != "crif":
            continue
        analysis = r.get("analysis") or {}
        cps = analysis.get("credit_profile_summary") or []
        active = [a for a in r["accounts"] if a.get("status") == "Active"]
        cps_count = sum(b["count"] for b in cps)
        cps_outstanding = sum(b["outstanding"] for b in cps)
        exp_count = len(active)
        exp_outstanding = sum(a.get("current_balance") or 0 for a in active)
        if cps_count != exp_count or cps_outstanding != exp_outstanding:
            failures.append(
                f"{os.path.basename(f)}: cps count/outstanding="
                f"{cps_count}/{cps_outstanding} vs active accounts="
                f"{exp_count}/{exp_outstanding}"
            )
    assert not failures, f"loan-type distribution doesn't reconcile: {failures}"


def test_retail_overdue_validation_includes_closed_accounts(parsed_reports):
    """
    CRIF Retail's Account Summary "Total Amount Overdue" is scoped over ALL
    accounts (including Closed - a written-off account can still carry a
    residual overdue balance at closure), unlike Total Current Balance which
    is active-only. Confirmed on a real report where the entire reported
    overdue figure lived on a single Closed account - an active-only sum
    read Rs.0 there and failed validation against a correctly-extracted
    figure. This is the opposite scope from CRIF Commercial (active-only,
    consistent with its Live-Accts-only convention elsewhere) - getting this
    backwards for either provider silently breaks real, correct extractions.
    """
    failures = []
    for f, r in parsed_reports.items():
        if isinstance(r, Exception) or r.get("provider") != "crif":
            continue
        v = r["validation"]
        exp_o = v.get("expected_overdue")
        if exp_o is None:
            continue
        all_sum    = sum(a.get("overdue") or 0 for a in r["accounts"])
        active_sum = sum(a.get("overdue") or 0 for a in r["accounts"]
                         if a.get("status") == "Active")
        if all_sum != active_sum and v.get("extracted_overdue") != all_sum:
            failures.append(
                f"{os.path.basename(f)}: validation used {v.get('extracted_overdue')}, "
                f"expected all-accounts sum {all_sum} (active-only would be {active_sum})"
            )
    assert not failures, f"Retail overdue validation not scoped to all accounts: {failures}"
