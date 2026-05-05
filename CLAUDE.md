# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

AutoCAM is a Streamlit app that extracts structured loan account data from CRIF High Mark and TransUnion CIBIL PDFs, validates the extraction against the report's own summary totals, and generates a formatted Excel file for credit analysts at Shriram Finance.

## Running the App

```bash
streamlit run app.py
```

API key for LLM fallback is loaded from `.env` (local) or Streamlit Secrets (deployed):
```
GEMINI_API_KEY=your_key_here
```

## Architecture

### File Responsibilities

| File | Role |
|---|---|
| `app.py` | Streamlit UI — upload, display metrics/table, download Excel |
| `parser.py` | Orchestrator — detect provider, validate, LLM fallback cascade |
| `crif_parser.py` | CRIF High Mark rule-based parsing |
| `tu_parser.py` | TransUnion commercial CIBIL rule-based parsing |
| `excel_generator.py` | Excel output with Shriram Finance formatting |

### Data Flow

```
PDF upload → extract_text() → _detect_provider()
    ├─ CRIF:       parse_crif()       → validate_extraction() → LLM fallback if needed
    └─ TransUnion: parse_transunion() → tu_validate()
                        ↓
            unified dict: {name, score, accounts, extraction_method, validation, provider}
                        ↓
                  generate_excel() → download bytes
```

### Provider Detection (`parser.py`)

Checks the first 5000 chars of the PDF text:
- "COMMERCIAL CREDIT INFORMATION REPORT", "TransUnion", "CIBIL MSME RANK", "CMR-" → TransUnion
- Everything else → CRIF

## CRIF Parsing Deep Dive (`crif_parser.py`)

### Account Block Splitting — Most Critical Logic

CRIF PDFs have 5 known formats for account headers. All patterns run simultaneously to handle mixed-format PDFs:

| Pattern | Format | Example |
|---|---|---|
| P1 | Number on next line | `Account Information\n3\n` |
| P2 | Blank line before number | `Account Information\n\n3\n` |
| P3 | Number on same line | `Account Information 3\n` |
| P4 | Number BEFORE header | `3\nAccount Information\n\nAccount Type:` |
| P5 | Number inline with Account Type | `Account Information\n20  Account Type:` |
| Pass 2 | Page-break recovery | Number swallowed by browser print header |

Pass 2 handles HTML-to-PDF page breaks where the browser inserts a timestamp/filename/page-number header between "Account Information" and the account number, making the number disappear entirely. The number is inferred from ordinal position.

### Closed Account Detection (`_is_closed`)

Three rules — any one is sufficient:
1. `Closed Date:` field has a `DD-MM-YYYY` value
2. `Remarks:` field (split across two lines as `Account\nRemarks:\n`) contains "Written-off"
3. `Closed` appears before any field label in a compact block (history-only blocks)

### Entity/Loan Type: Positional Lists

`build_positional_lists(text)` scans the entire text for ALL `Account Type:` and `Credit Grantor:` labels in document order. The Nth label maps to the Nth account block. This is more reliable than per-block extraction because CRIF's compact DPD summary table has clean labels before the detailed blocks.

### Account Summary Validation

CRIF's Account Summary table has 12 fixed columns. The last header keyword "Total Amount Overdue" precedes all 12 values in column order:
- Column index 1 = Active Accounts count
- Column index 6 = Total Current Balance

Validation compares active accounts only (CRIF's own note: "Current Balance is considered ONLY for ACTIVE accounts").

## TransUnion Parsing Deep Dive (`tu_parser.py`)

### Block Structure (Reversed from CRIF)

Each Credit Facility has a preamble that appears **BEFORE** the `Credit Facility N` header:
```
LAST REPORTED DATE : 30-SEP-2025          ← block start marker
SANCTIONED INR     INSTALLMENT AMOUNT
₹ 6,38,000         -                      ← two-column table, values follow both headers
OUTSTANDING BALANCE  SUIT FILED
₹ 5,65,275           -
...
Credit Facility 4                          ← header comes after amounts
Commercial vehicle loan
MEMBER : SHRIRAM FINANCE LIMITED
ASSET CLASSIFICATION/DPD : 0 DPD
```

Splits on `LAST REPORTED DATE : {date}` marker (one per facility).

### Validation

Uses `max()` of all `Total CF's` and `₹(100%)` values in the Credit Summary — picks the TOTAL row rather than YOUR INSTITUTION row.

## LLM Fallback (CRIF only, `parser.py`)

Triggers only when `validate_extraction()` returns `valid=False` AND `api_key` is available.

**Stage 2** — Block correction: sends raw block text + current extraction to Gemini, asks it to fix only wrong fields without adding/removing accounts.

**Stage 3** — Full extraction: sends first 28,000 chars of PDF text to Gemini with an account count hint.

Gemini models tried in order: `gemini-2.0-flash` → `gemini-2.0-flash-lite` → `gemini-1.5-flash` → `gemini-1.5-flash-latest`

TransUnion has no LLM fallback.

## Key Conventions

- **`_to_int(s)`** — shared helper in both parsers. Handles Indian comma format (1,23,456), floats, empty strings. Returns 0 on failure.
- **Date format** — CRIF uses `DD-MM-YYYY`; TransUnion uses `DD-MMM-YYYY` (e.g., 30-SEP-2025) which `_tu_date()` converts.
- **`page.get_text()`** — plain text mode is used (NOT `get_text("blocks")`). Blocks mode strips trailing newlines from each block, breaking the `Account Information\nN\n` patterns.
- **Negative balances** — both parsers support negative current balance (regex allows `-?[\d,]+`).
- **Provider field** — returned in the result dict as `"crif"` or `"transunion"`. Not currently shown prominently in UI but available.

## Excel Output (`excel_generator.py`)

Accepts the unified result dict from any parser. Key fields:
- `data["name"]` or `data["borrower_name"]` (both accepted)
- `data["score"]` or `data["cibil_score"]` (both accepted)
- `data["accounts"]` — list of account dicts

DPD colour coding: 0 = green, 1–30 = no colour, 31–90 = orange, >90 = red.

Score colour: >700 = green, 600–700 = orange, <600 = red.

The `key_points` field (list of strings) is rendered as a section below the accounts table if present; silently omitted if empty.
