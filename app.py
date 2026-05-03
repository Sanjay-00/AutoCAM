"""
app.py — AutoCAM CIBIL Analyzer | Shriram Finance
LangGraph + Gemini pipeline. Fixed: applymap→map, text colours, download.
"""

import streamlit as st
import pandas as pd
import traceback

from pipeline        import extract_cibil_data, load_api_key
from excel_generator import generate_excel, get_filename, _fmt_inr


st.set_page_config(
    page_title="AutoCAM – CIBIL Analyzer",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── CSS ──────────────────────────────────────────────────────────
st.markdown("""
<style>
    /* ── typography & titles ── */
    .main-title {
        font-family: Arial, sans-serif;
        font-size: 2rem;
        font-weight: 800;
        color: #1F3864;
        margin-bottom: 0;
    }
    .sub-title {
        font-size: 0.85rem;
        color: #555;
        letter-spacing: 0.08em;
        margin-bottom: 1.5rem;
    }

    /* ── section dividers ── */
    .section-header {
        font-size: 0.85rem;
        font-weight: 700;
        color: #1F3864;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        border-bottom: 2px solid #1F3864;
        padding-bottom: 4px;
        margin: 1.8rem 0 0.8rem 0;
    }

    /* ── CIBIL score pill ── */
    .score-pill {
        display: inline-block;
        padding: 8px 24px;
        border-radius: 6px;
        font-size: 2rem;
        font-weight: 900;
        letter-spacing: -0.02em;
    }
    .score-green  { background: #C6EFCE; color: #1E4620; }
    .score-orange { background: #FFEB9C; color: #7F4F00; }
    .score-red    { background: #FFC7CE; color: #8B0000; }

    /* ── risk routing badges ── */
    .badge-high {
        display: inline-block;
        background: #FFC7CE;
        color: #8B0000;
        padding: 5px 14px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.82rem;
        margin-top: 6px;
    }
    .badge-std {
        display: inline-block;
        background: #C6EFCE;
        color: #1E4620;
        padding: 5px 14px;
        border-radius: 4px;
        font-weight: 700;
        font-size: 0.82rem;
        margin-top: 6px;
    }

    /* ── risk flag boxes (amber) ── */
    .flag-box {
        background: #FFFBEA;
        border-left: 4px solid #F59E0B;
        color: #4B3800;
        padding: 8px 12px;
        margin: 4px 0;
        border-radius: 0 4px 4px 0;
        font-size: 0.85rem;
        font-weight: 500;
    }

    /* ── key point boxes ── */
    .kp-box {
        background: #EFF6FF;
        border-left: 4px solid #1F3864;
        color: #1E293B;
        padding: 9px 14px;
        margin: 5px 0;
        border-radius: 0 4px 4px 0;
        font-size: 0.87rem;
        line-height: 1.55;
    }
    .kp-risk {
        background: #FFF1F0;
        border-left: 4px solid #C0392B;
        color: #4A0000;
    }

    /* ── API key status badges ── */
    .key-ok {
        display: inline-block;
        background: #C6EFCE;
        color: #1E4620;
        padding: 5px 12px;
        border-radius: 4px;
        font-size: 0.78rem;
        font-weight: 700;
    }
    .key-err {
        display: inline-block;
        background: #FFC7CE;
        color: #8B0000;
        padding: 5px 12px;
        border-radius: 4px;
        font-size: 0.78rem;
        font-weight: 700;
    }

    /* ── download button ── */
    .stDownloadButton > button {
        background-color: #1F3864 !important;
        color: #FFFFFF !important;
        font-weight: 700 !important;
        font-size: 1rem !important;
        padding: 0.75rem 1.5rem !important;
        border-radius: 6px !important;
        border: none !important;
        width: 100% !important;
        margin-top: 8px;
    }
    .stDownloadButton > button:hover {
        background-color: #2d4f8c !important;
    }

    /* ── metric value text — ensure dark on light background ── */
    [data-testid="stMetricValue"] {
        color: #1F3864 !important;
        font-weight: 700 !important;
    }
    [data-testid="stMetricLabel"] {
        color: #444 !important;
    }
</style>
""", unsafe_allow_html=True)


# ── SIDEBAR ──────────────────────────────────────────────────────

with st.sidebar:
    st.markdown("## 📊 AutoCAM")
    st.markdown("---")

    try:
        load_api_key()
        st.markdown('<div class="key-ok">✓ Gemini API Key Loaded</div>',
                    unsafe_allow_html=True)
    except ValueError:
        st.markdown('<div class="key-err">✗ API Key Missing</div>',
                    unsafe_allow_html=True)
        st.error("Add `GEMINI_API_KEY=your_key` to your `.env` file.")

    st.markdown("---")
    st.markdown("### Pipeline")
    st.code(
        "extract_text\n"
        "     │\n"
        "parse_accounts  ← Gemini\n"
        "     │\n"
        " score_risk     ← Rules\n"
        "     │\n"
        " ┌───┴──────────┐\n"
        "std        high_risk\n"
        " └───┬──────────┘\n"
        "     │\n"
        "key_points  ← Gemini\n"
        "     │\n"
        " build_output → END",
        language=None
    )

    st.markdown("---")
    st.markdown("**Risk routing:**")
    st.markdown("🟢 Standard → 5 key points  \n🔴 High Risk → 6 points + referral")
    st.markdown("---")
    st.caption("Built by Sanjay Dutta · Shriram Finance · 2025")


# ── MAIN ─────────────────────────────────────────────────────────

st.markdown('<p class="main-title">📊 AutoCAM – CIBIL Analyzer</p>',
            unsafe_allow_html=True)
st.markdown('<p class="sub-title">SHRIRAM FINANCE LIMITED &nbsp;·&nbsp; LANGGRAPH + GEMINI</p>',
            unsafe_allow_html=True)

uploaded_file = st.file_uploader(
    "Upload CIBIL PDF Report",
    type=["pdf"],
    help="Upload a CRIF High Mark CIBIL PDF report",
)

# ── WELCOME ───────────────────────────────────────────────────────

if uploaded_file is None:
    c1, c2, c3 = st.columns(3)
    c1.info("**Step 1**\n\nAdd `GEMINI_API_KEY` to your `.env` file")
    c2.info("**Step 2**\n\nUpload a CIBIL PDF above")
    c3.info("**Step 3**\n\nDownload the formatted Excel report")
    st.markdown("---")
    st.markdown("""
**What AutoCAM does:**
- Extracts all loan accounts from any CRIF High Mark CIBIL PDF
- Calculates Max DPD per account from payment history grids
- Routes high-risk borrowers (overdue / DPD > 90 / score < 600) through deep analysis
- Generates AI-powered analyst key points via Gemini
- Outputs a formatted Excel file matching Shriram Finance analyst format
    """)
    st.stop()

# ── PRE-FLIGHT ────────────────────────────────────────────────────

try:
    load_api_key()
except ValueError as e:
    st.error(f"❌ {e}")
    st.stop()

col_btn, _ = st.columns([1, 3])
with col_btn:
    run = st.button("🚀 Run LangGraph Pipeline",
                    use_container_width=True, type="primary")

# ── PIPELINE EXECUTION ────────────────────────────────────────────

if run or st.session_state.get("processed"):

    if not st.session_state.get("processed"):
        with st.status("Running LangGraph pipeline…", expanded=True) as status:
            st.write("📄 **Node 1** — extract_text: reading PDF…")
            st.write("🤖 **Node 2** — parse_accounts: calling Gemini…")
            st.write("⚖️ **Node 3** — score_risk: evaluating risk flags…")
            st.write("🔀 **Node 4** — router: standard or high-risk path…")
            st.write("💬 **Node 5** — generate_key_points: writing analysis…")
            st.write("📊 **Node 6** — build_output: assembling report…")

            try:
                data = extract_cibil_data(uploaded_file)
                st.session_state["cibil_data"] = data
                st.session_state["processed"]  = True
                status.update(
                    label="✅ Pipeline complete!",
                    state="complete",
                    expanded=False
                )
            except ValueError as e:
                status.update(label="❌ Failed", state="error")
                st.error(f"**Extraction Error:** {e}")
                st.stop()
            except RuntimeError as e:
                status.update(label="❌ Failed", state="error")
                st.error(f"**Gemini API Error:** {e}")
                st.stop()
            except Exception as e:
                status.update(label="❌ Failed", state="error")
                st.error(f"**Pipeline Error:** {e}")
                with st.expander("Technical details"):
                    st.code(traceback.format_exc())
                st.stop()

    # ── RESULTS ──────────────────────────────────────────────────

    data       = st.session_state["cibil_data"]
    accounts   = data.get("accounts",        [])
    name       = data.get("borrower_name",   "Unknown")
    score      = data.get("cibil_score",     "NA")
    key_points = data.get("key_points",      [])
    risk_level = data.get("risk_level",      "standard")
    risk_flags = data.get("risk_flags",      [])

    st.success(f"✅ Analysis complete for **{name}**")

    # ── BORROWER SUMMARY ─────────────────────────────────────────

    st.markdown('<div class="section-header">Borrower Summary</div>',
                unsafe_allow_html=True)

    left_col, right_col = st.columns([3, 1])

    with left_col:
        st.markdown(f"#### {name}")

        # Risk badge
        if risk_level == "high_risk":
            st.markdown('<span class="badge-high">🔴 HIGH RISK — Deep Analysis Applied</span>',
                        unsafe_allow_html=True)
        else:
            st.markdown('<span class="badge-std">🟢 STANDARD RISK</span>',
                        unsafe_allow_html=True)

        st.markdown("")

        # Metrics row
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Total Accounts", len(accounts))
        m2.metric("Active",         data.get("active_count",  0))
        m3.metric("Closed",         data.get("closed_count",  0))
        m4.metric("Active Exposure",f"₹{_fmt_inr(data.get('total_exposure', 0))}")
        m5.metric("Total Overdue",  f"₹{_fmt_inr(data.get('total_overdue',  0))}")

    with right_col:
        try:
            sv = int(score)
            if   sv > 700: pill_cls, label = "score-green",  "GOOD"
            elif sv >= 600: pill_cls, label = "score-orange", "AVERAGE"
            else:           pill_cls, label = "score-red",    "POOR"
        except (TypeError, ValueError):
            sv, pill_cls, label = score, "score-orange", "N/A"

        st.markdown(
            f'<div style="text-align:center; padding-top:10px;">'
            f'<div style="font-size:0.7rem; color:#555; text-transform:uppercase; '
            f'letter-spacing:0.1em; margin-bottom:4px;">CRIF Score</div>'
            f'<div class="score-pill {pill_cls}">{sv}</div>'
            f'<div style="font-size:0.72rem; color:#555; margin-top:4px;">{label}</div>'
            f'</div>',
            unsafe_allow_html=True
        )

    # ── RISK FLAGS ────────────────────────────────────────────────

    if risk_flags:
        st.markdown('<div class="section-header">⚠️ Risk Flags</div>',
                    unsafe_allow_html=True)
        for flag in risk_flags:
            st.markdown(f'<div class="flag-box">⚑ &nbsp;{flag}</div>',
                        unsafe_allow_html=True)

    # ── ACCOUNTS TABLE ────────────────────────────────────────────

    st.markdown('<div class="section-header">Loan Accounts</div>',
                unsafe_allow_html=True)

    if accounts:
        rows = []
        for acc in accounts:
            dpd = acc.get("max_dpd", 0)
            dpd_display = (
                f"🔴 {dpd}" if dpd > 90
                else f"🟠 {dpd}" if dpd > 30
                else f"🟢 {dpd}"
            )
            rows.append({
                "Sr.No"           : acc.get("sr_no", ""),
                "Date of Sanction": acc.get("date_of_sanction", "NA"),
                "Sanction Amt (₹)": _fmt_inr(acc.get("sanction_amount", 0)),
                "Current Bal (₹)" : _fmt_inr(acc.get("current_balance", 0)),
                "EMI (₹)"         : _fmt_inr(acc.get("emi", 0)),
                "Overdue (₹)"     : _fmt_inr(acc.get("overdue", 0)),
                "Entity"          : acc.get("entity", "XXXX"),
                "Type of Loan"    : acc.get("type_of_loan", ""),
                "Max DPD"         : dpd_display,
                "Status"          : acc.get("status", "Active"),
            })

        df = pd.DataFrame(rows)

        # pandas >= 2.1 renamed applymap → map
        def _style_status(val):
            if val == "Active":
                return "color: #1E4620; font-weight: bold; background-color: #F0FFF4;"
            return "color: #444444; background-color: #F5F5F5;"

        try:
            # pandas >= 2.1
            styled = df.style.map(_style_status, subset=["Status"])
        except AttributeError:
            # pandas < 2.1 fallback
            styled = df.style.applymap(_style_status, subset=["Status"])

        st.dataframe(
            styled,
            use_container_width=True,
            height=min(450, 42 + len(accounts) * 36),
            hide_index=True,
        )

        # Alert banners
        high_dpd = sum(1 for a in accounts if a.get("max_dpd", 0) > 30)
        if high_dpd:
            st.warning(f"⚠️ {high_dpd} account(s) have Max DPD > 30 days — review before sanction.")
        if data.get("total_overdue", 0) == 0:
            st.success("✅ Zero overdue across all accounts.")
    else:
        st.warning("No accounts were extracted from this report.")

    # ── KEY POINTS ────────────────────────────────────────────────

    route_label = "High-Risk Deep Analysis" if risk_level == "high_risk" else "Standard Analysis"
    st.markdown(
        f'<div class="section-header">AI Key Points — {route_label}</div>',
        unsafe_allow_html=True
    )

    for i, pt in enumerate(key_points, 1):
        is_risk = any(
            w in pt.lower()
            for w in ["risk", "alert", "decline", "refer", "overdue",
                      "warning", "severe", "caution"]
        )
        box_cls = "kp-box kp-risk" if is_risk else "kp-box"
        st.markdown(
            f'<div class="{box_cls}"><b>{i}.</b> {pt}</div>',
            unsafe_allow_html=True
        )

    st.markdown("---")

    # ── DOWNLOAD ─────────────────────────────────────────────────

    st.markdown('<div class="section-header">📥 Download Excel Report</div>',
                unsafe_allow_html=True)

    try:
        xl_bytes = generate_excel(data)
        fname    = get_filename(name)

        # Prominent full-width download button
        st.download_button(
            label               = "⬇️  Download Excel Report",
            data                = xl_bytes,
            file_name           = fname,
            mime                = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width = True,
        )
        st.caption(
            f"📄 `{fname}` — includes all {len(accounts)} accounts, "
            f"{'6' if risk_level == 'high_risk' else '5'} AI key points, "
            f"colour-coded DPD, frozen headers"
        )

    except Exception as e:
        st.error(f"❌ Excel generation failed: {e}")
        with st.expander("Technical details"):
            st.code(traceback.format_exc())

    st.markdown("")

    # ── RESET ────────────────────────────────────────────────────

    if st.button("🔄 Analyze Another Report", use_container_width=False):
        for k in ("cibil_data", "processed"):
            st.session_state.pop(k, None)
        st.rerun()
