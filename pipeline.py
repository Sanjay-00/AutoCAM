"""
pipeline.py
AutoCAM – LangGraph Agentic Pipeline
Shriram Finance | Sanjay Dutta

Graph structure:
  extract_text
      │
  parse_accounts          ← Gemini: extract all loan accounts as structured JSON
      │
  score_risk              ← Rule-based: compute DPD, overdue, exposure, FOIR flags
      │
  ┌───┴────────────────────────────────────────────┐
  │ router (conditional edge)                       │
  │  • overdue > 0  OR  max_dpd > 90  OR score<600  │
  └───────────┬────────────────────────────────────┘
              │                    │
        [deep_risk]         [standard_path]   (just passes through)
              │                    │
              └────────┬───────────┘
                  generate_key_points   ← Gemini: analyst narrative
                       │
                  build_output          ← assemble final dict for Excel
"""

import os
import re
import json
import pdfplumber
from typing import TypedDict, Annotated, List, Optional
from dotenv import load_dotenv

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.messages import HumanMessage

from langgraph.graph import StateGraph, END

load_dotenv()

# ─────────────────────────────────────────────────────────────────
# 1. STATE DEFINITION
#    TypedDict that flows through every node in the graph.
#    Each node reads what it needs and writes its outputs back.
# ─────────────────────────────────────────────────────────────────

class CIBILState(TypedDict):
    # Input
    pdf_file:        object          # uploaded file object
    api_key:         str

    # After extract_text
    raw_text:        str
    text_chunks:     List[str]

    # After parse_accounts
    borrower_name:   str
    cibil_score:     object          # int or "NA"
    accounts:        List[dict]

    # After score_risk
    risk_level:      str             # "standard" | "high_risk"
    risk_flags:      List[str]       # list of triggered flag descriptions
    total_exposure:  int
    total_overdue:   int
    max_dpd_overall: int
    active_count:    int
    closed_count:    int

    # After key_points
    key_points:      List[str]

    # Final output
    result:          dict            # complete structured dict for excel_generator


MAX_CHARS = 28000

# ─────────────────────────────────────────────────────────────────
# 2. LLM FACTORY
# ─────────────────────────────────────────────────────────────────

MODELS = [
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-latest",
    "gemini-1.5-pro-latest",
]

def get_llm(api_key: str, temperature: float = 0.1) -> ChatGoogleGenerativeAI:
    """Return a ChatGoogleGenerativeAI instance, trying models in order."""
    for model in MODELS:
        try:
            llm = ChatGoogleGenerativeAI(
                model          = model,
                google_api_key = api_key,
                temperature    = temperature,
                max_tokens     = 8192,
            )
            return llm
        except Exception:
            continue
    raise RuntimeError(f"No Gemini model available. Tried: {MODELS}")


# ─────────────────────────────────────────────────────────────────
# 3. PROMPTS
# ─────────────────────────────────────────────────────────────────

PARSE_PROMPT = PromptTemplate.from_template("""
You are a credit analyst. Extract structured data from this CIBIL report chunk.
Return ONLY valid JSON — no explanation, no markdown fences.

{{
  "borrower_name": "full name",
  "cibil_score": "numeric score or NA",
  "accounts": [
    {{
      "sr_no": 1,
      "date_of_sanction": "DD-MM-YYYY or NA",
      "sanction_amount": "numeric or NA",
      "current_balance": "numeric or NA",
      "emi": "numeric monthly EMI or NA",
      "overdue": "numeric or 0",
      "entity": "lender name or XXXX",
      "type_of_loan": "loan type",
      "max_dpd": "highest DPD number across all payment months, 0 if clean",
      "status": "Active or Closed"
    }}
  ]
}}

For max_dpd: payment history format is DPD/STATUS (e.g. 042/XXX).
Extract the number before '/'. Return the maximum across all months for this account.
000 = 0 DPD (good). 999 = data not reported (ignore).

CIBIL TEXT:
{text}
""")

KEY_POINTS_STANDARD_PROMPT = PromptTemplate.from_template("""
You are a senior NBFC credit analyst at Shriram Finance.
Write exactly 5 concise key points for a Credit Appraisal Memo.

BORROWER: {borrower_name}
CIBIL SCORE: {cibil_score}
TOTAL ACCOUNTS: {total_accounts}
ACTIVE ACCOUNTS: {active_count}
CLOSED ACCOUNTS: {closed_count}
TOTAL ACTIVE EXPOSURE: Rs.{total_exposure}
TOTAL OVERDUE: Rs.{total_overdue}
MAX DPD EVER: {max_dpd_overall}
RISK FLAGS: {risk_flags}

Rules:
- Format: "Label: explanation." (label = 2-3 words, explanation = 1-2 sentences with numbers)
- Last point MUST be "Approval Condition: ..." with a concrete actionable recommendation
- Return ONLY a JSON array of 5 strings. No markdown, no extra text.
""")

KEY_POINTS_DEEP_RISK_PROMPT = PromptTemplate.from_template("""
You are a senior NBFC credit analyst at Shriram Finance.
This borrower has been flagged HIGH RISK. Write exactly 6 detailed key points.

BORROWER: {borrower_name}
CIBIL SCORE: {cibil_score}
TOTAL ACCOUNTS: {total_accounts}
ACTIVE ACCOUNTS: {active_count}
CLOSED ACCOUNTS: {closed_count}
TOTAL ACTIVE EXPOSURE: Rs.{total_exposure}
TOTAL OVERDUE: Rs.{total_overdue}
MAX DPD EVER: {max_dpd_overall}
TRIGGERED RISK FLAGS: {risk_flags}

Rules:
- Be specific and conservative — this is a high-risk profile
- Mention each triggered flag explicitly
- Include a clear "Recommendation:" point (Decline / Conditional Approval / Refer to Senior)
- Format: "Label: explanation." 
- Return ONLY a JSON array of 6 strings. No markdown, no extra text.
""")


# ─────────────────────────────────────────────────────────────────
# 4. HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────

def load_api_key() -> str:
    try:
        import streamlit as st
        key = st.secrets.get("GEMINI_API_KEY", "")
        if key and key not in ("", "your_key_here"):
            return key.strip()
    except Exception:
        pass
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key and key not in ("", "your_key_here", "your_gemini_api_key_here"):
        return key
    raise ValueError(
        "GEMINI_API_KEY not found.\n"
        "• Local: add GEMINI_API_KEY=your_key to .env\n"
        "• Streamlit Cloud: add in App Settings → Secrets"
    )


def chunk_text(text: str, max_chars: int = MAX_CHARS) -> List[str]:
    if len(text) <= max_chars:
        return [text]
    chunks = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text); break
        split_at = text.rfind("\n", 0, max_chars)
        if split_at == -1: split_at = max_chars
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def coerce_int(val, default=0) -> int:
    if val in (None, "NA", "", "N/A", "na"): return default
    try:
        return int(float(str(val).replace(",", "").replace("₹","").replace("Rs.","")))
    except (ValueError, TypeError):
        return default


def normalize_accounts(accounts: list) -> list:
    for acc in accounts:
        for f in ("sanction_amount", "current_balance", "emi", "overdue"):
            acc[f] = coerce_int(acc.get(f, 0))
        acc["max_dpd"] = coerce_int(acc.get("max_dpd", 0))
        acc["status"]  = "Active" if "active" in str(acc.get("status","")).lower() else "Closed"
        if not acc.get("date_of_sanction"): acc["date_of_sanction"] = "NA"
        if not acc.get("entity"):           acc["entity"] = "XXXX"
    return accounts


def merge_parsed(results: list) -> tuple:
    """Merge multi-chunk Gemini results into one (name, score, accounts)."""
    name  = ""
    score = "NA"
    all_accs = []
    seen_sr  = set()

    for r in results:
        if not name:       name  = r.get("borrower_name", "")
        if score == "NA":  score = r.get("cibil_score",   "NA")
        for acc in r.get("accounts", []):
            sr = acc.get("sr_no", 0)
            if sr not in seen_sr:
                all_accs.append(acc)
                seen_sr.add(sr)

    all_accs = sorted(all_accs, key=lambda x: int(x.get("sr_no", 0)))
    return name, score, all_accs


def safe_json_parse(text: str) -> dict:
    """Strip markdown fences and parse JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$",          "", text)
    return json.loads(text.strip())


def llm_call_with_fallback(api_key: str, prompt_text: str, temperature: float = 0.1) -> str:
    """Call the LLM, trying each model in MODELS until one works."""
    for model in MODELS:
        try:
            llm = ChatGoogleGenerativeAI(
                model          = model,
                google_api_key = api_key,
                temperature    = temperature,
                max_tokens     = 8192,
            )
            response = llm.invoke([HumanMessage(content=prompt_text)])
            return response.content
        except Exception as e:
            if "404" in str(e) or "NOT_FOUND" in str(e):
                continue   # try next model
            raise          # re-raise auth/quota errors
    raise RuntimeError(f"No Gemini model worked. Tried: {MODELS}")


# ─────────────────────────────────────────────────────────────────
# 5. GRAPH NODES
# ─────────────────────────────────────────────────────────────────

def node_extract_text(state: CIBILState) -> dict:
    """
    NODE 1 — extract_text
    Reads the uploaded PDF with pdfplumber and chunks it.
    Pure Python, no LLM call.
    """
    pages = []
    with pdfplumber.open(state["pdf_file"]) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t: pages.append(t)

    if not pages:
        raise ValueError("Could not extract text from PDF — may be scanned/image-based.")

    full_text = "\n".join(pages)
    return {
        "raw_text":    full_text,
        "text_chunks": chunk_text(full_text),
    }


def node_parse_accounts(state: CIBILState) -> dict:
    """
    NODE 2 — parse_accounts
    Sends each chunk to Gemini via LangChain.
    Uses JsonOutputParser for clean structured output.
    Merges multi-chunk results.
    """
    api_key = state["api_key"]
    results = []

    for chunk in state["text_chunks"]:
        prompt_text = PARSE_PROMPT.format(text=chunk)

        # Retry once with stricter instruction on JSON failure
        for attempt in range(2):
            try:
                raw = llm_call_with_fallback(api_key, prompt_text)
                parsed = safe_json_parse(raw)
                results.append(parsed)
                break
            except json.JSONDecodeError:
                if attempt == 0:
                    prompt_text += "\n\nReturn ONLY the JSON object. No text before or after."
                continue

    name, score, accounts = merge_parsed(results)

    # Normalise score
    try:
        score = int(str(score).replace(",", ""))
    except (ValueError, TypeError):
        score = "NA"

    return {
        "borrower_name": name or "Unknown",
        "cibil_score":   score,
        "accounts":      normalize_accounts(accounts),
    }


def node_score_risk(state: CIBILState) -> dict:
    """
    NODE 3 — score_risk
    Pure Python rule engine — no LLM.
    Computes aggregate metrics and decides routing.

    Risk flags triggered when:
      • CIBIL score < 600
      • Any account has overdue > 0
      • Max DPD across all accounts > 90
      • Active exposure > 1 Crore
      • More than 15 active accounts simultaneously
    """
    accounts = state["accounts"]
    active   = [a for a in accounts if a["status"] == "Active"]
    closed   = [a for a in accounts if a["status"] == "Closed"]

    total_exposure  = sum(a["current_balance"] for a in active)
    total_overdue   = sum(a["overdue"]         for a in accounts)
    max_dpd_overall = max((a["max_dpd"] for a in accounts), default=0)
    score           = state["cibil_score"]

    flags = []

    try:
        score_int = int(score)
        if score_int < 600:
            flags.append(f"Low CIBIL score ({score_int}) — below minimum threshold of 600")
        elif score_int < 700:
            flags.append(f"Below-average CIBIL score ({score_int}) — borderline, needs justification")
    except (TypeError, ValueError):
        flags.append("CIBIL score unavailable — cannot assess creditworthiness without score")

    if total_overdue > 0:
        flags.append(f"Active overdue of Rs.{total_overdue:,} — must be cleared before disbursement")

    if max_dpd_overall > 90:
        flags.append(f"Severe delinquency history — Max DPD {max_dpd_overall} days across accounts")
    elif max_dpd_overall > 30:
        flags.append(f"Moderate delinquency history — Max DPD {max_dpd_overall} days observed")

    if total_exposure > 10_000_000:   # > 1 Crore
        flags.append(f"Very high active exposure — Rs.{total_exposure:,} across {len(active)} accounts")
    elif total_exposure > 5_000_000:  # > 50 Lakh
        flags.append(f"High active exposure — Rs.{total_exposure:,} across {len(active)} accounts")

    if len(active) > 15:
        flags.append(f"Excessive concurrent obligations — {len(active)} active loans simultaneously")

    # Routing decision
    # HIGH RISK if any severe flag exists
    severe = any(kw in f for f in flags for kw in ["overdue", "Severe", "Low CIBIL", "unavailable"])
    risk_level = "high_risk" if severe else "standard"

    return {
        "risk_level":      risk_level,
        "risk_flags":      flags,
        "total_exposure":  total_exposure,
        "total_overdue":   total_overdue,
        "max_dpd_overall": max_dpd_overall,
        "active_count":    len(active),
        "closed_count":    len(closed),
    }


def node_standard_path(state: CIBILState) -> dict:
    """
    NODE 4a — standard_path
    Passthrough node for normal-risk borrowers.
    Just logs the routing decision — no processing needed.
    """
    return {}   # state passes through unchanged


def node_deep_risk(state: CIBILState) -> dict:
    """
    NODE 4b — deep_risk
    Triggered for high-risk borrowers.
    Augments risk_flags with per-account DPD breakdown for the LLM.
    """
    accounts = state["accounts"]

    # Add detail on every account with DPD > 30
    bad_accounts = [
        a for a in accounts if a.get("max_dpd", 0) > 30
    ]

    extra_flags = list(state["risk_flags"])
    for a in bad_accounts:
        extra_flags.append(
            f"Account #{a['sr_no']} ({a['type_of_loan']}) — "
            f"Max DPD {a['max_dpd']} days, "
            f"Status: {a['status']}, "
            f"Balance: Rs.{a['current_balance']:,}"
        )

    return {"risk_flags": extra_flags}


def node_generate_key_points(state: CIBILState) -> dict:
    """
    NODE 5 — generate_key_points
    Calls Gemini via LangChain to write analyst key points.
    Uses the deep-risk prompt if risk_level == 'high_risk'.
    """
    api_key    = state["api_key"]
    risk_level = state["risk_level"]

    # Choose prompt based on routing
    if risk_level == "high_risk":
        prompt_template = KEY_POINTS_DEEP_RISK_PROMPT
    else:
        prompt_template = KEY_POINTS_STANDARD_PROMPT

    prompt_text = prompt_template.format(
        borrower_name  = state["borrower_name"],
        cibil_score    = state["cibil_score"],
        total_accounts = state["active_count"] + state["closed_count"],
        active_count   = state["active_count"],
        closed_count   = state["closed_count"],
        total_exposure = f"{state['total_exposure']:,}",
        total_overdue  = f"{state['total_overdue']:,}",
        max_dpd_overall= state["max_dpd_overall"],
        risk_flags     = "; ".join(state["risk_flags"]) if state["risk_flags"] else "None",
    )

    # Retry once on JSON failure
    for attempt in range(2):
        try:
            raw    = llm_call_with_fallback(api_key, prompt_text, temperature=0.2)
            points = safe_json_parse(raw)
            if isinstance(points, list):
                return {"key_points": [str(p) for p in points]}
        except (json.JSONDecodeError, Exception):
            if attempt == 0:
                prompt_text += "\n\nReturn ONLY a JSON array of strings. Example: [\"point1\", \"point2\"]"
            continue

    # Fallback: rule-based key points
    return {"key_points": _fallback_key_points(state)}


def node_build_output(state: CIBILState) -> dict:
    """
    NODE 6 — build_output
    Assembles the final dict consumed by excel_generator.generate_excel().
    Adds risk_level and risk_flags as metadata.
    """
    result = {
        "borrower_name": state["borrower_name"],
        "cibil_score":   state["cibil_score"],
        "accounts":      state["accounts"],
        "key_points":    state["key_points"],
        # Extra metadata (not used by Excel but useful for Streamlit UI)
        "risk_level":    state["risk_level"],
        "risk_flags":    state["risk_flags"],
        "total_exposure":state["total_exposure"],
        "total_overdue": state["total_overdue"],
        "max_dpd_overall":state["max_dpd_overall"],
        "active_count":  state["active_count"],
        "closed_count":  state["closed_count"],
    }
    return {"result": result}


# ─────────────────────────────────────────────────────────────────
# 6. ROUTING FUNCTION (conditional edge)
# ─────────────────────────────────────────────────────────────────

def route_by_risk(state: CIBILState) -> str:
    """
    Conditional edge after score_risk.
    Returns the name of the next node to execute.
    """
    return state["risk_level"]   # "standard" or "high_risk"


# ─────────────────────────────────────────────────────────────────
# 7. BUILD THE GRAPH
# ─────────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    """
    Assemble and compile the LangGraph StateGraph.

    Graph topology:
      extract_text
           │
      parse_accounts
           │
       score_risk
           │
    ┌──────┴──────────┐
    standard_path   deep_risk
    └──────┬──────────┘
           │
    generate_key_points
           │
      build_output
           │
          END
    """
    graph = StateGraph(CIBILState)

    # Add nodes
    graph.add_node("extract_text",          node_extract_text)
    graph.add_node("parse_accounts",        node_parse_accounts)
    graph.add_node("score_risk",            node_score_risk)
    graph.add_node("standard_path",         node_standard_path)
    graph.add_node("deep_risk",             node_deep_risk)
    graph.add_node("generate_key_points",   node_generate_key_points)
    graph.add_node("build_output",          node_build_output)

    # Linear edges
    graph.add_edge("extract_text",        "parse_accounts")
    graph.add_edge("parse_accounts",      "score_risk")

    # Conditional edge: score_risk → standard_path OR deep_risk
    graph.add_conditional_edges(
        "score_risk",
        route_by_risk,
        {
            "standard":  "standard_path",
            "high_risk": "deep_risk",
        }
    )

    # Both paths converge at generate_key_points
    graph.add_edge("standard_path",       "generate_key_points")
    graph.add_edge("deep_risk",           "generate_key_points")
    graph.add_edge("generate_key_points", "build_output")
    graph.add_edge("build_output",        END)

    # Entry point
    graph.set_entry_point("extract_text")

    return graph.compile()


# ─────────────────────────────────────────────────────────────────
# 8. PUBLIC API  (called by app.py)
# ─────────────────────────────────────────────────────────────────

def extract_cibil_data(pdf_file) -> dict:
    """
    Main entry point. Runs the full LangGraph pipeline.
    Returns a dict ready for excel_generator.generate_excel().
    """
    api_key = load_api_key()
    graph   = build_graph()

    initial_state: CIBILState = {
        "pdf_file":        pdf_file,
        "api_key":         api_key,
        "raw_text":        "",
        "text_chunks":     [],
        "borrower_name":   "",
        "cibil_score":     "NA",
        "accounts":        [],
        "risk_level":      "standard",
        "risk_flags":      [],
        "total_exposure":  0,
        "total_overdue":   0,
        "max_dpd_overall": 0,
        "active_count":    0,
        "closed_count":    0,
        "key_points":      [],
        "result":          {},
    }

    final_state = graph.invoke(initial_state)
    return final_state["result"]


# ─────────────────────────────────────────────────────────────────
# 9. FALLBACK KEY POINTS (no LLM)
# ─────────────────────────────────────────────────────────────────

def _fallback_key_points(state: CIBILState) -> List[str]:
    pts = []
    score = state["cibil_score"]

    if state["total_overdue"] == 0:
        pts.append("Zero Overdues: Rs.0 overdue across all active accounts — clean current position.")
    else:
        pts.append(f"Overdue Alert: Rs.{state['total_overdue']:,} overdue — must be cleared before disbursement.")

    pts.append(
        f"Credit History: {state['active_count'] + state['closed_count']} total accounts; "
        f"{state['active_count']} active, {state['closed_count']} closed."
    )
    pts.append(
        f"Active Exposure: Total active portfolio of Rs.{state['total_exposure']:,} "
        f"across {state['active_count']} accounts."
    )

    if state["max_dpd_overall"] > 0:
        pts.append(
            f"Past Delinquency: Max DPD of {state['max_dpd_overall']} days historically observed. "
            f"Verify that all dues have been cleared and regularised."
        )
    else:
        pts.append("Clean Repayment: No significant DPD across all historical accounts.")

    risk = state["risk_level"]
    if risk == "high_risk":
        pts.append(
            f"Recommendation: HIGH RISK — {'; '.join(state['risk_flags'][:2])}. "
            f"Refer to senior credit manager before proceeding."
        )
    else:
        pts.append(
            f"Approval Condition: Profile appears acceptable. "
            f"Verify 6-month bank statements to confirm cash flow supports new EMI obligations."
        )

    return pts
