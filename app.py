"""
AutoCAM — CIBIL to Excel
Upload a CRIF CIBIL PDF → Download Excel with account data.
"""

import os
import streamlit as st
from parser import parse, METHOD_RULE_BASED, METHOD_LLM_CORRECTION, METHOD_LLM_FULL
from excel_generator import generate_excel, get_filename

st.set_page_config(page_title="CIBIL → Excel", page_icon="📊", layout="centered")
st.title("📊 CIBIL → Excel")


def _load_api_key() -> str | None:
    try:
        key = st.secrets.get("GEMINI_API_KEY", "")
        if key and key not in ("", "your_key_here"):
            return key.strip()
    except Exception:
        pass
    key = os.getenv("GEMINI_API_KEY", "").strip()
    return key if key and key not in ("", "your_key_here") else None


def _method_badge(method: str):
    if method == METHOD_RULE_BASED:
        st.success(f"✅  {method}")
    elif method == METHOD_LLM_CORRECTION:
        st.warning(f"⚠️  {method}")
    else:
        st.error(f"🤖  {method}")


def _validation_badge(v: dict):
    expected_count = v.get("expected_count")
    expected_bal   = v.get("expected_balance")

    if expected_count is None and expected_bal is None:
        st.info(
            f"ℹ️  Validation: no totals found in report — "
            f"extracted **{v['extracted_count']}** accounts, "
            f"balance **₹{v['extracted_balance']:,}**"
        )
        return

    if v["valid"]:
        lines = [f"✅  Validation passed — **{v['extracted_count']}** accounts"]
        if expected_count is not None:
            lines.append(f"Count: {v['extracted_count']} / {expected_count}")
        if expected_bal:
            lines.append(f"Balance: ₹{v['extracted_balance']:,} / ₹{expected_bal:,}")
        st.success("  ·  ".join(lines))
    else:
        st.warning("⚠️  Validation mismatch\n\n" + "\n\n".join(f"• {i}" for i in v["issues"]))


# ── UI ──────────────────────────────────────────────────────────
uploaded = st.file_uploader("Upload CRIF CIBIL PDF", type=["pdf"])

if uploaded:
    if st.button("Generate Excel", type="primary", use_container_width=True):
        with st.spinner("Extracting data..."):
            try:
                api_key = _load_api_key()
                data     = parse(uploaded, api_key=api_key)

                accounts = data["accounts"]
                active   = [a for a in accounts if a.get("status") == "Active"]
                closed   = [a for a in accounts if a.get("status") == "Closed"]

                name       = data["name"]
                score      = data["score"]
                method     = data["extraction_method"]
                validation = data["validation"]

                if not accounts:
                    st.warning("No accounts found in this CIBIL report.")
                else:
                    _method_badge(method)
                    _validation_badge(validation)

                    st.info(
                        f"**{name}**  ·  Score: **{score}**  ·  "
                        f"Total extracted: **{len(accounts)}**  ·  "
                        f"Active: **{len(active)}**  ·  Closed: **{len(closed)}**"
                    )

                    # Excel includes ALL accounts (active + closed) for review
                    excel_bytes = generate_excel(data)
                    fname = get_filename(name)

                    st.download_button(
                        label=f"⬇️  Download {fname}",
                        data=excel_bytes,
                        file_name=fname,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                    )

            except Exception as e:
                st.error(f"Error: {e}")
                import traceback
                st.code(traceback.format_exc())
