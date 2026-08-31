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
