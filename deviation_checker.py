"""
Deviation-approval checker for Shriram Finance's CV Policy Annexure-IV
("Bureau Validation", p.16). Pure policy calculator - no I/O, no
Streamlit/parser imports. Consumes the unified parser result dict
(provider/score/accounts) plus three analyst-entered loan attributes.

Policy source: CV POLICY v2 -26-03-26.pdf, Annexure-IV, page 16.
See docs/superpowers/specs/2026-08-31-deviation-approval-design.md for
the full transcription and the resolved bureau-scale caveats.
"""
import re

_APPROVAL_RANK = {None: 0, "BM": 1, "RBH": 2, "ZCC": 3, "BUCC": 4}

# Loan-type keywords excluded from Bureau Validation entirely, per the
# policy's own footnote ("KCC, Credit Card, Agri, Gold or any other
# revolving loan"). "any other revolving loan" beyond these four named
# categories can't be reliably detected from loan-type text alone and is
# a known, documented limitation - not handled here.
_EXCLUDED_LOAN_TYPE_KEYWORDS = ("KCC", "KISAN CREDIT CARD", "CREDIT CARD", "AGRI", "GOLD")
_EXCLUDED_OWNERSHIP = ("GUARANTOR", "JOINT")


def _is_excluded(account: dict) -> str | None:
    ownership = (account.get("ownership") or "").upper()
    for kw in _EXCLUDED_OWNERSHIP:
        if kw in ownership:
            return f"Ownership: {account.get('ownership')} (Guarantee/Joint capacity - policy excludes this)"
    loan_type = (account.get("type_of_loan") or "").upper()
    for kw in _EXCLUDED_LOAN_TYPE_KEYWORDS:
        if kw in loan_type:
            return f"Loan type: {account.get('type_of_loan')} (KCC/Credit Card/Agri/Gold - policy excludes this)"
    return None


_CV_PV_CE_MACHINERY_SLABS = [
    {"label": "Up to 20L",     "entity": "individual", "max_amt": 2_000_000, "min_score": 550, "overdue_threshold": 5000},
    {"label": ">20-50L",       "entity": "individual", "max_amt": 5_000_000, "min_score": 600, "overdue_threshold": 5000},
    {"label": ">50L",          "entity": "individual", "max_amt": None,      "min_score": 600, "overdue_threshold": 10000},
    {"label": "Non-Individual", "entity": "non_individual", "max_amt": None,  "min_cmr": 7,     "overdue_threshold": 5000},
]
_CV_PV_CE_MACHINERY_APPROVAL = {
    "score": "BM",
    "overdue_dpd": [(30, "RBH"), (60, "ZCC")],
    "dpd_12mo": "RBH",
    "last_dpd": "RBH",
    "written_off": "ZCC",
}

_PRIVATE_CAR_SLABS = [
    {"label": "<5L",           "entity": "individual", "max_amt": 500_000,   "min_score": 550, "overdue_threshold": 5000, "strict_below": True},
    {"label": "5-20L",         "entity": "individual", "max_amt": 2_000_000, "min_score": 600, "overdue_threshold": 5000},
    {"label": ">20-50L",       "entity": "individual", "max_amt": 5_000_000, "min_score": 600, "overdue_threshold": 5000},
    {"label": ">50L",          "entity": "individual", "max_amt": None,      "min_score": 600, "overdue_threshold": 10000},
    {"label": "Non-Individual", "entity": "non_individual", "max_amt": None,  "min_cmr": 7,     "overdue_threshold": 5000},
]
_PRIVATE_CAR_APPROVAL = {
    "score": "RBH",
    "overdue_dpd": [(30, "RBH"), (60, "ZCC")],
    "dpd_12mo": "ZCC",
    "last_dpd": "RBH",
    "written_off": "BUCC",
}

_TABLES = {
    "cv_pv_ce_machinery": (_CV_PV_CE_MACHINERY_SLABS, _CV_PV_CE_MACHINERY_APPROVAL),
    "private_car": (_PRIVATE_CAR_SLABS, _PRIVATE_CAR_APPROVAL),
}


def _pick_slab(slabs: list, entity_type: str, loan_amount: int) -> dict:
    if entity_type == "non_individual":
        return next(s for s in slabs if s["entity"] == "non_individual")
    candidates = [s for s in slabs if s["entity"] == "individual"]
    for s in candidates:
        if s.get("strict_below") and loan_amount < s["max_amt"]:
            return s
        if not s.get("strict_below") and s["max_amt"] is not None and loan_amount <= s["max_amt"]:
            return s
    return candidates[-1]  # last (uncapped, ">X") slab


def _escalate(value, tiers: list):
    """tiers: [(threshold, authority), ...] ascending. Returns the highest
    authority whose threshold `value` exceeds, or None if it clears all."""
    authority = None
    for threshold, tier_authority in tiers:
        if value is not None and value > threshold:
            authority = tier_authority
    return authority


def _score_status(data: dict, slab: dict):
    provider = data.get("provider")
    score = data.get("score")
    if provider == "crif_commercial":
        return {"status": "info_only", "value": score, "threshold": None, "authority": None}
    if "min_cmr" in slab:
        m = re.search(r"CMR-(\d+)", str(score) or "")
        if not m:
            return {"status": "unable_to_verify", "value": score, "threshold": slab["min_cmr"], "authority": None}
        cmr = int(m.group(1))
        passed = cmr <= slab["min_cmr"]
        return {"status": "pass" if passed else "fail", "value": score,
                "threshold": slab["min_cmr"], "authority": None if passed else "BM"}
    if not isinstance(score, (int, float)):
        return {"status": "unable_to_verify", "value": score, "threshold": slab["min_score"], "authority": None}
    passed = score >= slab["min_score"]
    return {"status": "pass" if passed else "fail", "value": score,
            "threshold": slab["min_score"], "authority": None}


def evaluate_deviation(data: dict, loan_amount: int, vehicle_category: str, entity_type: str) -> dict:
    slabs, approval = _TABLES[vehicle_category]
    slab = _pick_slab(slabs, entity_type, loan_amount)

    excluded_accounts, included_accounts = [], []
    for a in data.get("accounts", []):
        reason = _is_excluded(a)
        if reason:
            excluded_accounts.append({**a, "exclusion_reason": reason})
        else:
            included_accounts.append(a)

    if not included_accounts:
        # Every account excluded (e.g. borrower has only KCC/Gold loans) -
        # the policy's own "Above norms not applicable" carve-out applies
        # to the whole check, not just individual accounts.
        na = {"status": "not_applicable", "breaching_accounts": [], "authority": None}
        return {
            "slab": slab["label"],
            "excluded_accounts": excluded_accounts,
            "parameters": {
                "score": {"status": "not_applicable", "value": None, "threshold": None, "authority": None},
                "overdue_dpd": {**na, "unreadable_accounts": []},
                "dpd_12mo": {**na, "unreadable_accounts": []},
                "last_dpd": {**na, "unreadable_accounts": []},
                "written_off": {**na, "manual_review": []},
            },
            "overall_approval": None,
            "incomplete": False,
        }

    score_result = _score_status(data, slab)
    if score_result["status"] == "fail":
        score_result["authority"] = approval["score"]

    overdue_over_threshold = [
        a for a in included_accounts
        if (a.get("overdue") or 0) > slab["overdue_threshold"]
    ]
    overdue_unreadable = [a for a in overdue_over_threshold if a.get("last_reported_dpd") is None]
    overdue_breaches = [
        a for a in overdue_over_threshold
        if a.get("last_reported_dpd") is not None and a.get("last_reported_dpd") > 0
    ]
    overdue_worst_dpd = max((a.get("last_reported_dpd") for a in overdue_breaches), default=0)
    overdue_authority = _escalate(overdue_worst_dpd, approval["overdue_dpd"]) if overdue_breaches else None

    dpd12_unreadable = [a for a in included_accounts if a.get("max_dpd_12mo") is None]
    dpd12_breaches = [a for a in included_accounts if (a.get("max_dpd_12mo") or 0) > 90]

    lastdpd_unreadable = [a for a in included_accounts if a.get("last_reported_dpd") is None]
    lastdpd_breaches = [a for a in included_accounts if (a.get("last_reported_dpd") or 0) > 30]

    def _written_off_hit(a):
        if a.get("written_off"):
            return True
        if a.get("status") in ("Written Off", "Settled"):
            return True
        if a.get("suit_filed"):
            return True
        if a.get("max_dpd_12mo") is not None and a.get("max_dpd_12mo") >= 181:  # Loss bucket
            return True
        return False

    written_off_breaches = [a for a in included_accounts if _written_off_hit(a)]

    parameters = {
        "score": score_result,
        "overdue_dpd": {
            "status": (
                "unable_to_verify" if (overdue_unreadable and not overdue_breaches)
                else ("fail" if overdue_authority else "pass")
            ),
            "breaching_accounts": overdue_breaches,
            "unreadable_accounts": overdue_unreadable,
            "authority": overdue_authority,
        },
        "dpd_12mo": {
            "status": "unable_to_verify" if (dpd12_unreadable and not dpd12_breaches) else ("fail" if dpd12_breaches else "pass"),
            "breaching_accounts": dpd12_breaches,
            "unreadable_accounts": dpd12_unreadable,
            "authority": approval["dpd_12mo"] if dpd12_breaches else None,
        },
        "last_dpd": {
            "status": "unable_to_verify" if (lastdpd_unreadable and not lastdpd_breaches) else ("fail" if lastdpd_breaches else "pass"),
            "breaching_accounts": lastdpd_breaches,
            "unreadable_accounts": lastdpd_unreadable,
            "authority": approval["last_dpd"] if lastdpd_breaches else None,
        },
        "written_off": {
            "status": "fail" if written_off_breaches else "pass",
            "breaching_accounts": written_off_breaches,
            "authority": approval["written_off"] if written_off_breaches else None,
            "manual_review": written_off_breaches,  # >3yr-old + good-repayment carve-out is analyst judgment
        },
    }

    overall = None
    for p in parameters.values():
        auth = p.get("authority")
        if auth and _APPROVAL_RANK[auth] > _APPROVAL_RANK[overall]:
            overall = auth

    # overall_approval only aggregates `authority` values, which are also
    # None for every "unable_to_verify" parameter - so a report with
    # unreadable DPD data and no other breach would otherwise look
    # identical to a genuinely clean report. `incomplete` lets the UI tell
    # those two cases apart instead of showing a false "all clear".
    incomplete = any(p.get("status") == "unable_to_verify" for p in parameters.values())

    return {
        "slab": slab["label"],
        "excluded_accounts": excluded_accounts,
        "parameters": parameters,
        "overall_approval": overall,
        "incomplete": incomplete,
    }
