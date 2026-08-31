import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crif_commercial_parser import parse_crif_commercial

def test_account_dict_has_windowed_dpd_fields():
    block = (
        "Type:\n"
        "Commercial Vehicle Loan -In INR\n"
        "Account #: 12345\nAmount Overdue: 0\nSanctioned Amount: 10,00,000\n"
        "DPD/Asset\nClassification:\nSTANDARD\n"
        "Current Balance: 0\nCurrent Balance History (12 Months):\n"
        "Current Balance amounts in Lakhs\n"
        "Payment History/Asset Classification:\n010/xxx\n"
    )
    text = "Commercial ACE Report\nBorrower Summary\nLoan Terms For:\n" + block
    name, score, blocks, accounts, reported_totals, analysis = parse_crif_commercial(text)
    assert accounts, "expected at least one parsed account"
    a = accounts[0]
    assert "last_reported_dpd" in a
    assert "max_dpd_12mo" in a
    assert a["max_dpd_12mo"] == a["max_dpd"]
