"""
AutoCAM  -  CIBIL Report Analyser
CRIF High Mark PDF → Structured Excel
"""

import os
import pandas as pd
import streamlit as st
from parser import (
    parse, METHOD_RULE_BASED, METHOD_LLM_CORRECTION, METHOD_LLM_FULL,
    METHOD_OCR, METHOD_VISION,
)
from excel_generator import generate_excel, get_filename

st.set_page_config(
    page_title="AutoCAM  -  CIBIL",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
    [data-testid="stMetricValue"] { font-size: 1.7rem !important; font-weight: 700 !important; }
    [data-testid="stMetricLabel"] { font-size: 0.78rem !important; color: #666; }
    .stTabs [data-baseweb="tab"] { font-size: 0.88rem; padding: 0.35rem 1.1rem; }
    hr { margin: 0.8rem 0 !important; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────

def _load_api_key():
    try:
        key = st.secrets.get("GEMINI_API_KEY", "")
        if key and key not in ("", "your_key_here"):
            return key.strip()
    except Exception:
        pass
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return key if key and key not in ("", "your_key_here") else None


def _fmt_inr(n) -> str:
    try:
        n = int(n)
        if n == 0:
            return "₹0"
        s = str(abs(n))
        r = s[-3:]
        s = s[:-3]
        while s:
            r = s[-2:] + "," + r
            s = s[:-2]
        return ("₹-" if int(n) < 0 else "₹") + r
    except Exception:
        return str(n)


def _score_badge(score):
    try:
        s = int(score)
        if s >= 750:
            color, label = "#1e7e34", "Good"
        elif s >= 650:
            color, label = "#856404", "Average"
        else:
            color, label = "#721c24", "Poor"
        st.markdown(
            f'<div style="display:inline-block;background:{color};color:white;'
            f'padding:0.3rem 1rem;border-radius:8px;font-size:1.5rem;font-weight:700;">'
            f'{score} <span style="font-size:0.85rem;opacity:0.85">({label})</span></div>',
            unsafe_allow_html=True,
        )
    except Exception:
        st.markdown(f"**Score:** {score}")


def _method_badge(method):
    cfg = {
        METHOD_RULE_BASED:     st.success, METHOD_LLM_CORRECTION: st.warning,
        METHOD_LLM_FULL:       st.error,   METHOD_OCR:            st.warning,
        METHOD_VISION:         st.info,
    }
    icons = {
        METHOD_RULE_BASED: "✅", METHOD_LLM_CORRECTION: "⚠️", METHOD_LLM_FULL: "🤖",
        METHOD_OCR:        "📄", METHOD_VISION:         "👁️",
    }
    cfg.get(method, st.info)(f"{icons.get(method, 'ℹ️')}  {method}")


def _validation_badge(v):
    exp_c, exp_b = v.get("expected_count"), v.get("expected_balance")
    if exp_c is None and exp_b is None:
        st.info("ℹ️  Validation: summary section not found in this PDF")
        return

    # Current balance is the authoritative check; active count is secondary (the
    # bureau itself sometimes mislabels active/closed, and a misclassified
    # zero-balance account doesn't move the balance total).
    bal_ok = (not exp_b) or abs((v.get("extracted_balance") or 0) - exp_b) <= max(exp_b * 0.05, 1000)
    cnt_ok = (exp_c is None) or (v.get("extracted_count") == exp_c)

    lines = []
    if exp_b:
        lines.append(f"{'✅' if bal_ok else '❌'}  Current balance: "
                     f"{_fmt_inr(v['extracted_balance'])} / {_fmt_inr(exp_b)} (summary)")
    if exp_c is not None:
        lines.append(f"{'✅' if cnt_ok else '⚠️'}  Active accounts: "
                     f"{v.get('extracted_count')} / {exp_c} (summary)")
    body = "\n\n".join(lines)

    if bal_ok and cnt_ok:
        st.success("✅  Validation passed\n\n" + body)
    elif bal_ok:
        st.warning("⚠️  Balance matches the summary, but the active count differs  -  "
                   "often a bureau active/closed labelling difference. Please review.\n\n" + body)
    else:
        st.error("❌  Balance does not match the summary  -  review the extraction.\n\n" + body)


def _to_df(accounts: list) -> pd.DataFrame:
    return pd.DataFrame([{
        "Sr.No":            a["sr_no"],
        "Date of Sanction": a["date_of_sanction"],
        "Sanction Amt":     a["sanction_amount"],
        "Current Balance":  a["current_balance"],
        "EMI":              a["emi"],
        "Overdue":          a["overdue"],
        "Entity":           a["entity"],
        "Ownership":        a.get("ownership", ""),
        "Type of Loan":     a["type_of_loan"],
        "Max DPD":          a["max_dpd"],
        "Status":           a["status"],
    } for a in accounts])


_COL_CFG = {
    "Sr.No":            st.column_config.NumberColumn("Sr.No",            width="small"),
    "Date of Sanction": st.column_config.TextColumn(  "Date of Sanction", width="medium"),
    "Sanction Amt":     st.column_config.NumberColumn("Sanction Amt (₹)", format="₹%d"),
    "Current Balance":  st.column_config.NumberColumn("Current Bal (₹)",  format="₹%d"),
    "EMI":              st.column_config.NumberColumn("EMI (₹)",           format="₹%d"),
    "Overdue":          st.column_config.NumberColumn("Overdue (₹)",       format="₹%d"),
    "Entity":           st.column_config.TextColumn(  "Entity"),
    "Ownership":        st.column_config.TextColumn(  "Ownership",         width="small"),
    "Type of Loan":     st.column_config.TextColumn(  "Type of Loan"),
    "Max DPD":          st.column_config.NumberColumn("Max DPD",           width="small"),
    "Status":           st.column_config.TextColumn(  "Status",            width="small"),
}


# ── Page header ───────────────────────────────────────────────────
st.markdown("## 🏦 AutoCAM &nbsp;·&nbsp; CIBIL Report Analyser")
st.caption("Upload a CRIF High Mark CIBIL PDF · Extracts structured loan data · Outputs Excel")
st.divider()

# ── Upload & trigger ──────────────────────────────────────────────
uploaded = st.file_uploader("Upload CRIF CIBIL PDF", type=["pdf"], label_visibility="visible")

col_btn, _ = st.columns([1, 4])
with col_btn:
    run = st.button("🔍  Extract Data", type="primary", use_container_width=True, disabled=not uploaded)

if not (uploaded and run):
    st.stop()

# ── Parse ─────────────────────────────────────────────────────────
_progress_bar  = st.progress(0, text="Reading PDF…")
_status_text   = st.empty()


def _on_ocr_progress(current: int, total: int):
    pct = int(current / total * 100)
    _progress_bar.progress(pct, text=f"Scanning page {current} of {total}…")
    _status_text.caption(
        f"OCR in progress · {current}/{total} pages done"
        + ("  -  this takes a few minutes for large scanned reports" if current == 1 else "")
    )


try:
    data = parse(uploaded, api_key=_load_api_key(), on_progress=_on_ocr_progress)
except Exception as e:
    _progress_bar.empty()
    _status_text.empty()
    st.error(f"Parsing failed: {e}")
    import traceback
    st.code(traceback.format_exc())
    st.stop()

_progress_bar.empty()
_status_text.empty()

accounts = data["accounts"]
active   = [a for a in accounts if a["status"] == "Active"]
closed   = [a for a in accounts if a["status"] == "Closed"]
name     = data["name"]
score    = data["score"]

total_overdue = sum(a["overdue"]         for a in active)
total_balance = sum(a["current_balance"] for a in active)
max_dpd_all   = max((a["max_dpd"] for a in accounts), default=0)

# ── Borrower + Score ──────────────────────────────────────────────
st.divider()
left, right = st.columns([3, 1])
with left:
    st.markdown(f"### 👤 {name}")
with right:
    _score_badge(score)
st.divider()

# ── Key metrics  -  row 1 ───────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total Accounts",  len(accounts))
c2.metric("Active",          len(active))
c3.metric("Closed",          len(closed))
c4.metric("Active Balance",  _fmt_inr(total_balance))
c5.metric("Total Overdue",   _fmt_inr(total_overdue))

# row 2
d1, d2, d3, d4, d5 = st.columns(5)
d1.metric("Max DPD (ever)",         f"{max_dpd_all} days")
d2.metric("Accounts w/ Overdue",    sum(1 for a in active   if a["overdue"] > 0))
d3.metric("Accounts w/ DPD",        sum(1 for a in accounts if a["max_dpd"] > 0))
d4.metric("Avg Active Balance",     _fmt_inr(total_balance // len(active)) if active else "₹0")
d5.metric("Total Exposure",         _fmt_inr(sum(a["sanction_amount"] for a in active)))

st.divider()

# ── Extraction & Validation status ───────────────────────────────
s1, s2 = st.columns(2)
with s1:
    _method_badge(data["extraction_method"])
with s2:
    _validation_badge(data["validation"])

st.divider()

# ── Account table ─────────────────────────────────────────────────
st.markdown("#### 📋 Account Details")

tab_all, tab_active, tab_closed = st.tabs([
    f"All  ({len(accounts)})",
    f"🟢 Active  ({len(active)})",
    f"⚫ Closed  ({len(closed)})",
])

with tab_all:
    st.dataframe(_to_df(accounts), column_config=_COL_CFG, use_container_width=True, hide_index=True)
with tab_active:
    if active:
        st.dataframe(_to_df(active), column_config=_COL_CFG, use_container_width=True, hide_index=True)
    else:
        st.info("No active accounts.")
with tab_closed:
    if closed:
        st.dataframe(_to_df(closed), column_config=_COL_CFG, use_container_width=True, hide_index=True)
    else:
        st.info("No closed accounts.")

if data["extraction_method"] == METHOD_OCR:
    st.caption(
        "ℹ️ **Max DPD is approximate** on scanned reports  -  the dense payment-history "
        "grid has tiny digits that OCR can misread. Verify against the PDF for any "
        "delinquent (non-zero DPD) account before relying on it."
    )

st.divider()

# ── Download ──────────────────────────────────────────────────────
_, dl_col, _ = st.columns([1, 3, 1])
with dl_col:
    excel_bytes = generate_excel(data)
    fname       = get_filename(name)
    st.download_button(
        label=f"⬇️  Download Excel   -   {fname}",
        data=excel_bytes,
        file_name=fname,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
        type="primary",
    )