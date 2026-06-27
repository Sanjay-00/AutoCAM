# AutoCAM - CIBIL Report Analyser



## The Problem

At Shriram Finance, credit analysts prepare a **CAM (Credit Appraisal Memo)** for every customer with an exposure above Rs. 25 lakh. A critical section of the CAM requires manually listing all active loans from the customer's CIBIL report, including sanction amount, outstanding balance, EMI, overdue, DPD, and lender details.

This is straightforward for a customer with 3-4 loans. But CIBIL reports with **50-100+ loan accounts** make this a slow, error-prone, and repetitive task, done 6-7 times per month per branch.

## The Solution

AutoCAM eliminates this manual effort entirely.

Upload a CIBIL PDF and get a structured, formatted Excel file in seconds.

- Extracts all loan accounts automatically (active and closed)
- Covers **CRIF High Mark** (retail + Commercial ACE), and **TransUnion CIBIL** report formats
- Reads **scanned / image-based** reports too, via OCR (Tesseract) with a Gemini Vision fallback
- Validates extracted data against the report's own summary totals
- Outputs an Excel file in the exact format required for the CAM
- Filter by Active / Closed accounts directly in the app or excel

**Live demo: [autocam-cibil.streamlit.app](https://autocam-cibil.streamlit.app/)**


## Screenshots

**Upload screen**

![Upload UI](assets/ui_upload.png)

**After extraction - borrower profile and key metrics**

![Metrics](assets/ui_metrics.png)

**Account table with Active / Closed filter and Excel download**

![Table and Download](assets/ui_table.png)

## Impact

| Before | After |
|---|---|
| 30-60 min manual data entry per CAM | Under 1 minute |
| Risk of transcription errors | Validated against report totals |
| Only feasible for small CIBIL reports | Handles 100+ account reports |
| Done atleast 6-7 times/month per branch | Same frequency, fraction of the effort |

## How it works

1. Upload a digital CIBIL PDF (CRIF High Mark or TransUnion)
2. Click **Extract Data**
3. Review the dashboard - borrower name, CIBIL score, account metrics
4. Download the pre-formatted Excel file ready for the CAM

If rule-based extraction doesn't pass validation, the app automatically falls back to a Gemini LLM for correction, without any action needed from the user. Scanned reports are OCR'd with Tesseract first, and fall back to Gemini Vision (on the relevant pages only) when the OCR'd parse doesn't reconcile with the report totals.

## Tech Stack

Streamlit · PyMuPDF · Tesseract / pytesseract · openpyxl · LangChain + Google Gemini (text + vision) · pandas

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

OCR of scanned reports needs the **Tesseract** engine installed:

- **Windows**: install the [UB Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki). The app auto-detects `C:\Program Files\Tesseract-OCR\tesseract.exe`; for a custom path set the `TESSERACT_CMD` environment variable.
- **Linux / Streamlit Cloud**: handled automatically via `packages.txt` (`tesseract-ocr`).

The Gemini API key is read from `.env` (`GEMINI_API_KEY=...`) locally or Streamlit Secrets when deployed.

## Future Work

- **LLM-powered credit analysis**: auto-generate risk observations and key points from the extracted CIBIL data
- Multi-borrower batch processing

## Limitations

- OCR of a large scanned report (100+ pages) takes a few minutes
- TransUnion reports have no LLM fallback
