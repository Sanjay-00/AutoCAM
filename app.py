"""
AutoCAM  -  CIBIL Report Analyser
CRIF High Mark PDF → Structured Excel
"""

import os
import re
from collections import Counter
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
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
    # CRIF Commercial isn't a 300-900 score - it's a 1 (best) - 5 (worst) risk
    # rank rendered as "N (Label)", e.g. "5 (Very High Risk)". Colour it on
    # its own scale (low rank = good = green) rather than falling through to
    # the plain-text branch below, which used to show it unstyled.
    m = re.match(r'^\s*(\d)\s*\(', str(score))
    if m:
        rank = int(m.group(1))
        if rank <= 2:
            color = "#1e7e34"
        elif rank == 3:
            color = "#856404"
        else:
            color = "#721c24"
        st.markdown(
            f'<div style="display:inline-block;background:{color};color:white;'
            f'padding:0.3rem 1rem;border-radius:8px;font-size:1.5rem;font-weight:700;">'
            f'{score}</div>',
            unsafe_allow_html=True,
        )
        return
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


_CHECK_CIBIL = "Check CIBIL"

def _amt(v):
    """Return value as-is if numeric, or 'Check CIBIL' sentinel if None."""
    return _CHECK_CIBIL if v is None else v

def _status_display(a: dict) -> str:
    # CRIF Commercial only: Delinquent/Suit Filed are independent overlay
    # flags on top of the canonical status (Active/Closed/Written Off/
    # Settled) - same composition excel_generator uses, kept identical so
    # the on-screen table matches the downloaded Excel exactly.
    flags = []
    if a.get("delinquent"):
        flags.append("Delinquent")
    if a.get("suit_filed"):
        flags.append("Suit Filed")
    status = a["status"]
    return f"{status} ({', '.join(flags)})" if flags else status


def _to_df(accounts: list) -> pd.DataFrame:
    return pd.DataFrame([{
        "Sr.No":            a["sr_no"],
        "Date of Sanction": a["date_of_sanction"],
        "Sanction Amt":     _amt(a["sanction_amount"]),
        "Current Balance":  _amt(a["current_balance"]),
        "EMI":              a["emi"],
        "Overdue":          a["overdue"],
        "Entity":           a["entity"],
        "Ownership":        a.get("ownership", ""),
        "Type of Loan":     a["type_of_loan"],
        "Max DPD":          _amt(a["max_dpd"]),
        "Status":           _status_display(a),
    } for a in accounts])


_COL_CFG = {
    "Sr.No":            st.column_config.NumberColumn("Sr.No",            width="small"),
    "Date of Sanction": st.column_config.TextColumn(  "Date of Sanction", width="medium"),
    "Sanction Amt":     st.column_config.TextColumn(  "Sanction Amt (₹)"),
    "Current Balance":  st.column_config.TextColumn(  "Current Bal (₹)"),
    "EMI":              st.column_config.NumberColumn("EMI (₹)",           format="₹%d"),
    "Overdue":          st.column_config.NumberColumn("Overdue (₹)",       format="₹%d"),
    "Entity":           st.column_config.TextColumn(  "Entity"),
    "Ownership":        st.column_config.TextColumn(  "Ownership",         width="small"),
    "Type of Loan":     st.column_config.TextColumn(  "Type of Loan"),
    "Max DPD":          st.column_config.TextColumn(  "Max DPD",           width="small"),
    "Status":           st.column_config.TextColumn(  "Status",            width="small"),
}


# ── Page header ───────────────────────────────────────────────────
st.markdown("## 🏦 AutoCAM &nbsp;·&nbsp; CIBIL Report Analyser")
st.caption("Upload a CRIF High Mark or TransUnion CIBIL PDF or HTML report · Extracts structured loan data · Outputs Excel")
st.divider()

# ── Upload & trigger ──────────────────────────────────────────────
uploaded = st.file_uploader("Upload CRIF or TransUnion CIBIL PDF or HTML", type=["pdf", "html", "htm"], label_visibility="visible")

col_btn, col_dpd, _ = st.columns([1, 2, 2])
with col_btn:
    run = st.button("🔍  Extract Data", type="primary", use_container_width=True, disabled=not uploaded)
with col_dpd:
    use_vision_dpd = st.checkbox(
        "Use Gemini Vision fallback",
        value=False,
        help="Off by default - normal rule-based OCR runs first for scanned CRIF Commercial "
             "reports. Tick this to let Gemini step in when needed: (1) re-extracts accounts "
             "from page images if OCR fails the report's own summary validation, and "
             "(2) reads DPD from coloured payment-history cells OCR can't read. "
             "Adds ~15s and costs ~₹0.17 per report. Re-run extraction after ticking.",
    )

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


def _on_dpd_progress(done: int, total: int):
    _progress_bar.progress(100, text=f"Enriching DPD via Vision… page {done} of {total}")
    _status_text.caption(
        f"Reading coloured payment-history cells with Gemini Vision · {done}/{total} pages"
    )


try:
    data = parse(uploaded, api_key=_load_api_key(), on_progress=_on_ocr_progress,
                 on_dpd_progress=_on_dpd_progress if use_vision_dpd else None,
                 enrich_dpd=use_vision_dpd)
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
# "Closed" here means "not Active" - CRIF Commercial has Written Off/Settled
# too, and every account must land in Active or here so the tab counts
# always add up to the total (no account silently disappears from both).
closed   = [a for a in accounts if a["status"] != "Active"]
name     = data["name"]
score    = data["score"]

total_overdue = sum(a["overdue"] or 0                       for a in active)
total_balance = sum(a["current_balance"] or 0               for a in active)
max_dpd_all   = max((a["max_dpd"] for a in accounts if a["max_dpd"] is not None), default=0)
unread_dpd    = sum(1 for a in accounts if a["max_dpd"] is None)

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
d3.metric("Accounts w/ DPD",        sum(1 for a in accounts if (a["max_dpd"] or 0) > 0))
d4.metric("Avg Active Balance",     _fmt_inr(total_balance // len(active)) if active else "₹0")
d5.metric("Total Exposure",         _fmt_inr(sum(a["sanction_amount"] or 0 for a in active)))

if unread_dpd:
    st.caption(f"⚠️ Max DPD could not be read for **{unread_dpd} account(s)** - shown as "
               f"\"Check CIBIL\" in the table below.")

st.divider()

# ── Extraction & Validation status ───────────────────────────────
s1, s2 = st.columns(2)
with s1:
    _method_badge(data["extraction_method"])
    tv = data.get("tesseract_version")
    if tv:
        # Local installs and Streamlit Cloud's unpinned tesseract-ocr apt package
        # can resolve to different builds from identical code - if the same PDF
        # extracts differently here than on another machine, compare this value
        # first before assuming it's a code bug.
        st.caption(f"OCR engine: Tesseract {tv}")
with s2:
    _validation_badge(data["validation"])

# ── Gemini Vision fallback / enrichment status ────────────────────
if data.get("vision_fallback_used"):
    if data["extraction_method"] == METHOD_VISION:
        st.info("👁️  **Gemini Vision fallback was used** - rule-based OCR failed the "
                "report's own summary validation, so accounts were re-extracted from page "
                "images (adopted because it validated better).")
    else:
        st.info("👁️  Gemini Vision fallback was tried (OCR had failed validation) but its "
                "result didn't validate any better, so the rule-based OCR extraction was kept.")
elif data.get("vision_fallback_recommended"):
    st.warning(
        "⚠️  This scanned report **failed summary validation** with normal OCR. "
        "Tick **\"Use Gemini Vision fallback\"** above and re-run extraction to let "
        "Gemini re-read the account pages directly."
    )

if data.get("dpd_vision_used"):
    pages   = data.get("dpd_vision_pages", [])
    checked = data.get("dpd_vision_checked", [])
    patched = data.get("dpd_vision_patched", [])
    if pages:
        st.info(
            f"👁️  **Gemini Vision DPD enrichment was used**  -  sent **{len(pages)} page(s)** "
            f"covering **{len(checked)} account(s)** where OCR couldn't read the "
            f"payment-history grid at all, and resolved **{len(patched)}** of them."
        )
        with st.expander("Show page / account numbers"):
            st.markdown(f"**Pages sent:** {', '.join(map(str, pages))}")
            st.markdown(f"**Accounts checked (Sr.No):** {', '.join(map(str, checked))}")
            st.markdown(
                f"**Accounts resolved (Sr.No):** {', '.join(map(str, patched))}"
                if patched else "No additional delinquency was found."
            )
    else:
        st.info("👁️  Gemini Vision DPD enrichment ran, but every account already had a "
                "readable Max DPD from OCR - nothing to send.")
elif data.get("dpd_vision_recommended"):
    st.warning(
        "⚠️  This is a **scanned report** - OCR can misread DPD in coloured "
        "payment-history cells. Tick **\"Use Gemini Vision fallback\"** above "
        "and re-run extraction for more reliable Max DPD values."
    )

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
        breakdown = Counter(a["status"] for a in closed)
        if len(breakdown) > 1:
            st.caption("  ·  ".join(f"**{v}** {k}" for k, v in breakdown.items()))
        st.dataframe(_to_df(closed), column_config=_COL_CFG, use_container_width=True, hide_index=True)
    else:
        st.info("No closed accounts.")

if data["extraction_method"] == METHOD_OCR:
    st.caption(
        "ℹ️ **Max DPD is approximate** on scanned reports  -  the dense payment-history "
        "grid has tiny digits that OCR can misread. Verify against the PDF for any "
        "delinquent (non-zero DPD) account before relying on it."
    )

# ── Credit Analysis  (CRIF Commercial only)  ───────────────────────
analysis = data.get("analysis")
if analysis:
    st.divider()
    st.markdown("#### 📑 Credit Analysis")

    bs = analysis.get("borrower_summary") or {}
    your_inst  = bs.get("your_institution")  or {}
    other_inst = bs.get("other_institution") or {}

    st.caption("Our exposure vs the market")
    b1, b2, b3, b4 = st.columns(4)
    b1.metric("Our Live Accounts",      your_inst.get("live_accts", "NA"))
    b2.metric("Our Outstanding",        _fmt_inr(your_inst.get("outstanding_amt")) if your_inst.get("outstanding_amt") is not None else "NA")
    b3.metric("Market Live Accounts",   other_inst.get("live_accts", "NA"))
    b4.metric("Market Outstanding",     _fmt_inr(other_inst.get("outstanding_amt")) if other_inst.get("outstanding_amt") is not None else "NA")

    if bs.get("length_of_credit_history") or bs.get("new_accts_12m") is not None:
        st.caption(
            f"Credit history: **{bs.get('length_of_credit_history', 'NA')}**"
            f"  ·  New accounts (12m): **{bs.get('new_accts_12m', 'NA')}**"
            f"  ·  New delinquent (12m): **{bs.get('new_delinquent_accts_12m', 'NA')}**"
        )

    cps = analysis.get("credit_profile_summary") or []
    derog = analysis.get("derog_summary") or {}
    col_cps, col_derog = st.columns(2)
    with col_cps:
        st.caption("Asset-class distribution (active accounts)")
        if cps:
            st.dataframe(
                pd.DataFrame([{
                    "Asset Class": b["asset_class"],
                    "Accounts":    b["count"],
                    "Outstanding": _fmt_inr(b["outstanding"]),
                } for b in cps]),
                hide_index=True, use_container_width=True,
            )
        else:
            st.caption("No active accounts to classify.")
    with col_derog:
        st.caption("Derogatory status rollup")
        # Written Off / Suit Filed show original exposure (sanction amount) -
        # the bureau zeroes current_balance once an account reaches either
        # status, so labelling the column plainly "Amount" would otherwise
        # read as "Rs.0 impact". Settled / Delinquent show current balance,
        # which is still meaningful for those two.
        _DEROG_LABEL = {
            "written_off": "Written Off (orig. exposure)",
            "suit_filed":  "Suit Filed (orig. exposure)",
            "settled":     "Settled (balance)",
            "delinquent":  "Delinquent (balance)",
        }
        derog_rows = [
            (_DEROG_LABEL.get(k, k.replace("_", " ").title()), v["count"], v["amount"])
            for k, v in derog.items() if v["count"] > 0
        ]
        if derog_rows:
            st.dataframe(
                pd.DataFrame([{
                    "Category": r[0], "Accounts": r[1], "Amount": _fmt_inr(r[2]),
                } for r in derog_rows]),
                hide_index=True, use_container_width=True,
            )
        else:
            st.caption("No written-off, settled, suit-filed, or delinquent accounts.")

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