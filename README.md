# AutoCAM  -  CIBIL Report Analyser

**Live demo: [autocam-cibil.streamlit.app](https://autocam-cibil.streamlit.app/)**

---

## The Problem

At NBFCs, credit analysts prepare a **CAM (Credit Appraisal Memo)** for every customer with exposure above ₹25 lakh. A critical section requires manually listing every active loan from the customer's CIBIL report  -  sanction amount, outstanding balance, EMI, overdue, DPD, and lender  -  formatted to a specific layout.

For a customer with 5-15 loans this takes 10-15 minutes. For a customer with **50-100+ loan accounts**, it takes 30-60 minutes of careful copy-paste, done 20-30 times per month per branch. One transposition error in a balance figure can affect the credit decision.

## The Solution

AutoCAM eliminates this entirely. Upload a CIBIL PDF → get a formatted, validated Excel file in under a minute.

- Extracts all loan accounts automatically (active and closed)
- Covers **CRIF High Mark Retail**, **CRIF Commercial ACE**, and **TransUnion CIBIL** formats
- Reads **scanned / image-only** reports via OCR, not just digital PDFs
- **Self-validates** every extraction against the report's own summary totals before delivering results
- Falls back to Gemini LLM automatically when rule-based extraction doesn't reconcile  -  no user action needed
- Outputs an Excel file in the exact format required for the CAM, with DPD colour coding

---

## Impact

| Metric | Before | After |
|---|---|---|
| Time per CAM (CIBIL section) | 30-60 min manual entry | < 1 minute |
| Transcription risk | High  -  manual copy-paste from PDF | Eliminated  -  validated against report totals |
| Reports with 50+ accounts | Impractical to do accurately | Handled reliably |
| Scanned / emailed PDFs | Not processable | OCR'd and extracted automatically |
| Analyst trust in output | No way to verify without re-reading PDF | Validation badge shows pass/fail against bureau's own numbers |

---

## Screenshots

**Upload screen**

![Upload UI](assets/ui_upload.png)

**Borrower profile and key metrics after extraction**

![Metrics](assets/ui_metrics.png)

**Account table with Active / Closed filter and Excel download**

![Table and Download](assets/ui_table.png)

---

## How It Works (User Flow)

1. Upload a CIBIL PDF (digital or scanned)
2. Click **Extract Data**
3. Review the dashboard  -  borrower name, score, account count, validation badge
4. Filter by Active / Closed accounts
5. Download the pre-formatted Excel file, ready to paste into the CAM

---

## How It's Built

### Architecture

```
PDF upload
    │
    ├─ Digital PDF? → PyMuPDF text extraction (instant)
    └─ Scanned PDF? → Tesseract OCR (parallel, page-by-page)
                            │
                            ▼
                   Provider detection
                   (CRIF Retail / CRIF Commercial / TransUnion)
                            │
                            ▼
                   Rule-based text parsing
                   (regex + positional extraction)
                            │
                            ▼
              Self-validation against report's own summary
               ├─ PASS → deliver result
               └─ FAIL + API key → Gemini LLM correction (stage 2 or 3)
                            │
                            ▼
                   Formatted Excel output
```

### Key Engineering Decisions

**Geometry-aware OCR reconstruction**
CRIF reports use a multi-column grid layout. Tesseract's default reading order splits label/value pairs across lines (`Current Balance:` ends up far from `12,34,382`). Instead of `image_to_string`, the app uses `image_to_data` (word-level bounding boxes) and re-clusters words into visual rows by y-coordinate  -  reuniting every label with its value on the same line. This was the critical fix that made scanned CRIF reports extractable.

**Self-validation as a trust layer**
Every CRIF report contains its own Account Summary table with pre-calculated totals. The app extracts these numbers and compares them against the sum of extracted account balances. A green badge means the extraction is confirmed against the bureau's own arithmetic  -  the analyst doesn't need to verify anything manually. This is what makes the output trustworthy in a regulated lending context.

**Parallel OCR pipeline**
A 180-page Commercial ACE report took ~400 seconds serially. The app now pipelines rendering (main thread, MuPDF single-threaded) with OCR (worker pool, Tesseract subprocess releases the GIL). Result: **~123 seconds on the same report  -  3.25× faster**  -  with byte-identical output by construction.

**LLM as fallback, not primary**
Rule-based parsing is free, instant, and deterministic. LLM correction (Gemini) is only triggered when validation fails. Stage 2 sends only the problematic account blocks; Stage 3 sends the full document. Cascades through 4 Gemini model versions for resilience against quota limits.

**Colored margin strip detection**
CRIF Commercial ACE marks each account's status with a vertical colored strip in the left margin (red = active, green = closed). The app detects this via NumPy pixel analysis on the rendered page image and injects a status token into the OCR text stream  -  more reliable than the (often blank) Closure Reason / Closed Date fields.

### Tech Stack

| Layer | Technology |
|---|---|
| Web app | Streamlit |
| PDF text extraction | PyMuPDF (fitz) |
| OCR engine | Tesseract via pytesseract |
| Parallel OCR | Python `ThreadPoolExecutor` (pipelined) |
| Image processing | Pillow, NumPy |
| LLM fallback | Google Gemini (text + vision) via `google-generativeai` |
| Excel generation | openpyxl |
| Deployment | Streamlit Cloud |

---

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

**Tesseract** (required for scanned PDFs):
- **Windows**: install the [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki). The app auto-detects `C:\Program Files\Tesseract-OCR\tesseract.exe`; for a custom path set `TESSERACT_CMD`.
- **Linux / Streamlit Cloud**: installed automatically via `packages.txt`.

**Gemini API key** (optional  -  needed only for LLM fallback):  
Set `GEMINI_API_KEY=...` in a `.env` file locally, or in Streamlit Secrets when deployed.

---

## Limitations

- OCR of a large scanned report (100+ pages) takes 2-3 minutes on a 2-core machine
- Max DPD on scanned reports is best-effort  -  Tesseract accuracy on dense payment history grids is ~80-85%
- TransUnion reports have no LLM fallback (digital parsing only)
