# AutoCAM — CIBIL Report Analyser

A Streamlit app that extracts structured loan account data from **CRIF High Mark** and **TransUnion CIBIL** PDF reports, validates the extraction against the report's own summary totals, and generates a formatted Excel file for credit analysts.

Built for Shriram Finance credit analysis workflows.

---

## Features

- **Dual-provider support** - handles both CRIF High Mark and TransUnion commercial CIBIL PDFs with automatic detection
- **Rule-based extraction** - regex-driven parsing that handles 5+ known CRIF PDF layout variants, including HTML-to-PDF page-break edge cases
- **Self-validating** - compares extracted account counts and balances against the report's own Account Summary / Credit Summary totals
- **LLM fallback** (CRIF only) - if rule-based extraction fails validation, escalates through Gemini models for block-level correction or full-PDF extraction
- **Formatted Excel output** - Shriram Finance analyst format with DPD colour coding, Indian number formatting, CIBIL score badge, and a SUMIF-powered active exposure total
- **Key metrics dashboard** - active/closed account counts, total balance, overdue, max DPD, average active balance, and total exposure

---

## Tech Stack

| Layer | Library |
|---|---|
| UI | Streamlit |
| PDF parsing | PyMuPDF (fitz) |
| Excel generation | openpyxl |
| LLM fallback | LangChain + Google Gemini |
| Data handling | pandas |

---

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url>
cd CIBIL_EXCEL
pip install -r requirements.txt
```

### 2. Configure API key (optional)

The Gemini API key is only needed for LLM fallback, the app works without it if rule-based extraction passes validation.

**Local development** - create a `.env` file:
```
GEMINI_API_KEY=your_key_here
```

**Streamlit Cloud deployment** - add to `.streamlit/secrets.toml`:
```toml
GEMINI_API_KEY = "your_key_here"
```

### 3. Run

```bash
streamlit run app.py
```

---

## How It Works

```
PDF upload → extract_text() → _detect_provider()
    ├─ CRIF:       parse_crif()       → validate_extraction() → LLM fallback if needed
    └─ TransUnion: parse_transunion() → validate()
                        ↓
            unified dict: {name, score, accounts, extraction_method, validation, provider}
                        ↓
                  generate_excel() → download bytes
```

### Provider Detection

Checks the first 5000 characters of the PDF text for TransUnion markers (`"TransUnion"`, `"CIBIL MSME RANK"`, `"CMR-"`, `"COMMERCIAL CREDIT INFORMATION REPORT"`). Everything else is treated as CRIF.

### Extraction Methods

The app shows a badge indicating which method was used:

| Badge | Meaning |
|---|---|
| ✅ Rule-based extraction | All accounts found and validated by regex alone |
| ⚠️ LLM correction used | Block-level Gemini correction fixed a mismatch |
| 🤖 Full LLM extraction used | Full-PDF Gemini extraction was required |

### Validation

- **CRIF**: active account count + active current balance vs the report's Account Summary 12-column table
- **TransUnion**: total account count + active balance vs the Credit Summary `Total CF's` and `₹(100%)` rows
- Tolerance: 5% or ₹1,000, whichever is larger

---

## File Structure

```
├── app.py               # Streamlit UI — upload, metrics, table, download
├── parser.py            # Orchestrator — provider detection, validation, LLM fallback
├── crif_parser.py       # CRIF High Mark rule-based parser
├── tu_parser.py         # TransUnion commercial CIBIL parser
├── excel_generator.py   # Excel output with Shriram Finance formatting
├── requirements.txt
└── .env                 # (local only, gitignored) GEMINI_API_KEY
```

---

## Excel Output Format

The generated `.xlsx` file includes:

- **Row 1**: Borrower name
- **Row 2**: CIBIL score with colour coding (green > 700, orange 600–700, red < 600)
- **Row 4**: Column headers (navy background)
- **Rows 5+**: One row per loan account with:
  - DPD colour coding: green = 0, orange = 31–90, red > 90
  - Status colour: green = Active, grey = Closed
  - Indian number formatting (₹1,23,456)
- **Total row**: `SUMIF`-powered active exposure sum
- **Key points section**: Optional analyst notes (if provided by LLM)

Frozen panes on row 4 and landscape print layout are applied automatically.

---

## Debug Utility

To inspect how a PDF is being split into account blocks without running the UI:

```bash
python parser.py path/to/report.pdf
```

Prints block count, expected vs extracted counts, and the first 120 characters of each block with balance and entity.

---

## Requirements

- Python 3.9+
- Digital (text-based) PDF — scanned/image PDFs are not supported
- Gemini API key only needed if LLM fallback is required

---

## Known Limitations

- Scanned PDFs (image-only) will raise an error — only digital CIBIL PDFs with extractable text are supported
- TransUnion parser has no LLM fallback; rule-based extraction is the only path
- LLM fallback accuracy depends on Gemini availability and the quality of the raw PDF text
