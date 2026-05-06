# AutoCAM - CIBIL Report Analyser

Extracts structured loan account data from **CRIF High Mark** and **TransUnion CIBIL** PDFs and generates a formatted Excel file for credit analysts at Shriram Finance.

**Live demo - [autocam-cibil.streamlit.app](https://autocam-cibil.streamlit.app/)**

---

## How it works

1. Upload a digital CIBIL PDF (CRIF High Mark or TransUnion)
2. Click **Extract Data**
3. Review the metrics dashboard and account table
4. Download the formatted Excel file

---

## Screenshots

**Upload screen**

![Upload UI](assets/ui_upload.png)

**After extraction - metrics dashboard**

![Metrics](assets/ui_metrics.png)

**Account table and Excel download**

![Table and Download](assets/ui_table.png)

---

## Features

- Auto-detects CRIF High Mark vs TransUnion CIBIL reports
- Validates extracted data against the report's own summary totals
- LLM fallback via Google Gemini if rule-based extraction fails validation (CRIF only)
- Excel output with DPD colour coding, Indian number formatting, and active exposure total
- Dashboard with 10 key metrics: active/closed counts, balance, overdue, max DPD, exposure

---

## Setup

```bash
git clone [text](https://github.com/Sanjay-00/CAMpilot.git)
cd CIBIL_EXCEL
pip install -r requirements.txt
streamlit run app.py
```

**API key** (optional - only needed if LLM fallback is required):

```
# .env
GEMINI_API_KEY=your_key_here
```

For Streamlit Cloud, add it under **Settings - Secrets**.

---

## Tech Stack

Streamlit · PyMuPDF · openpyxl · LangChain + Google Gemini · pandas

---

## Future Work

- **LLM-powered CIBIL analysis** - automated credit risk commentary and key observations generated from extracted account data using an LLM
- Support for scanned/image-based PDFs via OCR
- Multi-borrower batch processing

---

## Limitations

- Scanned (image-based) PDFs are not supported - only digital CIBIL reports with extractable text
- Only works with 2 specific format - CRIF and TransUnion
