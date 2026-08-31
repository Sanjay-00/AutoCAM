import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from deviation_checker import evaluate_deviation


def _account(**kw):
    base = {
        "sr_no": 1, "overdue": 0, "current_balance": 0,
        "max_dpd_12mo": 0, "last_reported_dpd": 0,
        "status": "Active", "written_off": False, "suit_filed": False,
        "ownership": "INDIVIDUAL", "type_of_loan": "COMMERCIAL VEHICLE LOAN",
    }
    base.update(kw)
    return base


def test_clean_report_needs_no_approval():
    data = {"provider": "crif", "score": 700, "accounts": [_account()]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["overall_approval"] is None
    assert v["parameters"]["score"]["status"] == "pass"


def test_score_below_minimum_fails_with_bm():
    data = {"provider": "crif", "score": 500, "accounts": [_account()]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["parameters"]["score"]["status"] == "fail"
    assert v["parameters"]["score"]["authority"] == "BM"
    assert v["overall_approval"] == "BM"


def test_overdue_with_dpd_escalates_rbh_then_zcc():
    data_rbh = {"provider": "crif", "score": 700, "accounts": [
        _account(overdue=6000, last_reported_dpd=45)
    ]}
    v_rbh = evaluate_deviation(data_rbh, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_rbh["parameters"]["overdue_dpd"]["authority"] == "RBH"

    data_zcc = {"provider": "crif", "score": 700, "accounts": [
        _account(overdue=6000, last_reported_dpd=65)
    ]}
    v_zcc = evaluate_deviation(data_zcc, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_zcc["parameters"]["overdue_dpd"]["authority"] == "ZCC"
    assert v_zcc["overall_approval"] == "ZCC"


def test_dpd_12mo_over_90_flags_rbh_on_cv_table_zcc_on_private_car():
    data = {"provider": "crif", "score": 700, "accounts": [_account(max_dpd_12mo=120)]}
    v_cv = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_cv["parameters"]["dpd_12mo"]["authority"] == "RBH"

    v_car = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="private_car", entity_type="individual")
    assert v_car["parameters"]["dpd_12mo"]["authority"] == "ZCC"


def test_written_off_flags_manual_review_and_zcc_on_cv_table():
    data = {"provider": "crif", "score": 700, "accounts": [
        _account(status="Closed", written_off=True)
    ]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["parameters"]["written_off"]["status"] == "fail"
    assert v["parameters"]["written_off"]["authority"] == "ZCC"
    assert len(v["parameters"]["written_off"]["manual_review"]) == 1


def test_guarantor_and_kcc_accounts_excluded():
    data = {"provider": "crif", "score": 700, "accounts": [
        _account(sr_no=1, ownership="GUARANTOR", overdue=999999, last_reported_dpd=999),
        _account(sr_no=2, type_of_loan="KISAN CREDIT CARD", overdue=999999, last_reported_dpd=999),
        _account(sr_no=3),  # clean, included
    ]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    excluded_sr_nos = {a["sr_no"] for a in v["excluded_accounts"]}
    assert excluded_sr_nos == {1, 2}
    assert v["overall_approval"] is None


def test_unreadable_dpd_marks_unable_to_verify_not_pass():
    data = {"provider": "crif", "score": 700, "accounts": [
        _account(max_dpd_12mo=None, last_reported_dpd=None)
    ]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["parameters"]["dpd_12mo"]["status"] == "unable_to_verify"
    assert v["parameters"]["last_dpd"]["status"] == "unable_to_verify"


def test_incomplete_flag_true_when_dpd_unreadable():
    # DPD grids unreadable -> parameters report unable_to_verify, not pass -
    # overall_approval stays None (no confirmed breach) but `incomplete`
    # must flag that this is NOT the same as a clean report (Finding 1).
    data = {"provider": "crif", "score": 700, "accounts": [
        _account(last_reported_dpd=None, max_dpd_12mo=None)
    ]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["overall_approval"] is None
    assert v["incomplete"] is True


def test_incomplete_flag_false_on_fully_clean_report():
    data = {"provider": "crif", "score": 700, "accounts": [_account()]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["overall_approval"] is None
    assert v["incomplete"] is False


def test_non_individual_uses_cmr_threshold():
    data = {"provider": "transunion", "score": "CMR-8", "accounts": [_account(ownership="NON-INDIVIDUAL")]}
    v = evaluate_deviation(data, loan_amount=10_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="non_individual")
    assert v["parameters"]["score"]["status"] == "fail"  # CMR-8 worse than threshold CMR-7
    assert v["slab"] == "Non-Individual"

    data_ok = {"provider": "transunion", "score": "CMR-7", "accounts": [_account(ownership="NON-INDIVIDUAL")]}
    v_ok = evaluate_deviation(data_ok, loan_amount=10_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="non_individual")
    assert v_ok["parameters"]["score"]["status"] == "pass"


def test_all_accounts_excluded_marks_parameters_not_applicable():
    data = {"provider": "crif", "score": 700, "accounts": [
        _account(type_of_loan="GOLD LOAN"),
    ]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["parameters"]["overdue_dpd"]["status"] == "not_applicable"
    assert v["parameters"]["score"]["status"] == "not_applicable"
    assert v["overall_approval"] is None


def test_crif_commercial_score_is_info_only():
    data = {"provider": "crif_commercial", "score": "3 (High Risk)", "accounts": [_account()]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="non_individual")
    assert v["parameters"]["score"]["status"] == "info_only"


# --- Slab-boundary tests (design-spec Testing section gap) -----------------

def test_loan_amount_slab_boundaries():
    data = {"provider": "crif", "score": 700, "accounts": [_account()]}

    # cv_pv_ce_machinery: "Up to 20L" is inclusive (<=), exactly 20L stays in it.
    v_20l = evaluate_deviation(data, loan_amount=2_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_20l["slab"] == "Up to 20L"
    v_20l_plus1 = evaluate_deviation(data, loan_amount=2_000_001, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_20l_plus1["slab"] == ">20-50L"

    # cv_pv_ce_machinery: ">20-50L" is inclusive (<=), exactly 50L stays in it.
    v_50l = evaluate_deviation(data, loan_amount=5_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_50l["slab"] == ">20-50L"
    v_50l_plus1 = evaluate_deviation(data, loan_amount=5_000_001, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_50l_plus1["slab"] == ">50L"

    # private_car: "<5L" is strict/exclusive - exactly 5L falls into "5-20L", not "<5L".
    v_5l = evaluate_deviation(data, loan_amount=500_000, vehicle_category="private_car", entity_type="individual")
    assert v_5l["slab"] == "5-20L"
    v_5l_minus1 = evaluate_deviation(data, loan_amount=499_999, vehicle_category="private_car", entity_type="individual")
    assert v_5l_minus1["slab"] == "<5L"


def test_score_boundaries_550_and_600():
    # "Up to 20L" slab: min_score 550, boundary is inclusive pass (>=).
    v_549 = evaluate_deviation({"provider": "crif", "score": 549, "accounts": [_account()]},
                                loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_549["parameters"]["score"]["status"] == "fail"
    v_550 = evaluate_deviation({"provider": "crif", "score": 550, "accounts": [_account()]},
                                loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_550["parameters"]["score"]["status"] == "pass"

    # ">20-50L" slab: min_score 600, boundary is inclusive pass (>=).
    v_599 = evaluate_deviation({"provider": "crif", "score": 599, "accounts": [_account()]},
                                loan_amount=3_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_599["parameters"]["score"]["status"] == "fail"
    v_600 = evaluate_deviation({"provider": "crif", "score": 600, "accounts": [_account()]},
                                loan_amount=3_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_600["parameters"]["score"]["status"] == "pass"


def test_overdue_threshold_boundary_exactly_5000_and_10000_not_breach():
    # Policy says "> Rs.5000" - exactly at the threshold is NOT a breach.
    data_5000 = {"provider": "crif", "score": 700, "accounts": [
        _account(overdue=5000, last_reported_dpd=45)
    ]}
    v_5000 = evaluate_deviation(data_5000, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_5000["parameters"]["overdue_dpd"]["status"] == "pass"
    assert v_5000["parameters"]["overdue_dpd"]["breaching_accounts"] == []

    # >50L slab uses a 10000 overdue threshold - exactly at it is also not a breach.
    data_10000 = {"provider": "crif", "score": 700, "accounts": [
        _account(overdue=10000, last_reported_dpd=45)
    ]}
    v_10000 = evaluate_deviation(data_10000, loan_amount=6_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_10000["slab"] == ">50L"
    assert v_10000["parameters"]["overdue_dpd"]["status"] == "pass"
    assert v_10000["parameters"]["overdue_dpd"]["breaching_accounts"] == []


def test_dpd_escalation_boundaries_30_60_90():
    # overdue_dpd: tiers are (30, RBH), (60, ZCC), both "greater than" thresholds.
    v_30 = evaluate_deviation(
        {"provider": "crif", "score": 700, "accounts": [_account(overdue=6000, last_reported_dpd=30)]},
        loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_30["parameters"]["overdue_dpd"]["authority"] is None
    assert v_30["parameters"]["overdue_dpd"]["status"] == "pass"  # breach exists but no tier reached (ruling #2)

    v_31 = evaluate_deviation(
        {"provider": "crif", "score": 700, "accounts": [_account(overdue=6000, last_reported_dpd=31)]},
        loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_31["parameters"]["overdue_dpd"]["authority"] == "RBH"
    assert v_31["parameters"]["overdue_dpd"]["status"] == "fail"

    v_60 = evaluate_deviation(
        {"provider": "crif", "score": 700, "accounts": [_account(overdue=6000, last_reported_dpd=60)]},
        loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_60["parameters"]["overdue_dpd"]["authority"] == "RBH"

    v_61 = evaluate_deviation(
        {"provider": "crif", "score": 700, "accounts": [_account(overdue=6000, last_reported_dpd=61)]},
        loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_61["parameters"]["overdue_dpd"]["authority"] == "ZCC"

    # dpd_12mo: threshold is "> 90" - exactly 90 is not a breach, 91 is.
    v_dpd12_90 = evaluate_deviation(
        {"provider": "crif", "score": 700, "accounts": [_account(max_dpd_12mo=90)]},
        loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_dpd12_90["parameters"]["dpd_12mo"]["status"] == "pass"

    v_dpd12_91 = evaluate_deviation(
        {"provider": "crif", "score": 700, "accounts": [_account(max_dpd_12mo=91)]},
        loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_dpd12_91["parameters"]["dpd_12mo"]["status"] == "fail"


def test_overdue_breach_with_unreadable_dpd_is_unable_to_verify_not_silent_pass():
    # overdue exceeds the threshold but last_reported_dpd is None (unreadable) -
    # must NOT be silently coerced to DPD=0 and passed.
    data = {"provider": "crif", "score": 700, "accounts": [
        _account(overdue=6000, last_reported_dpd=None)
    ]}
    v = evaluate_deviation(data, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v["parameters"]["overdue_dpd"]["status"] == "unable_to_verify"
    assert v["parameters"]["overdue_dpd"]["breaching_accounts"] == []
    assert len(v["parameters"]["overdue_dpd"]["unreadable_accounts"]) == 1
    assert v["parameters"]["overdue_dpd"]["authority"] is None

    # A confidently-read DPD of 0 with an overdue breach is NOT a breach either
    # (the "Overdue with DPD" condition needs concurrent DPD).
    data_confident_zero = {"provider": "crif", "score": 700, "accounts": [
        _account(overdue=6000, last_reported_dpd=0)
    ]}
    v_zero = evaluate_deviation(data_confident_zero, loan_amount=1_000_000, vehicle_category="cv_pv_ce_machinery", entity_type="individual")
    assert v_zero["parameters"]["overdue_dpd"]["status"] == "pass"
    assert v_zero["parameters"]["overdue_dpd"]["unreadable_accounts"] == []
