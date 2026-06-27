"""
ocr_extractor.py — scanned-PDF front-end for AutoCAM.

A scanned CIBIL report carries no extractable text. This module:
  1. detects that case (is_scanned),
  2. OCRs every page to text with Tesseract (ocr_document) so the normal
     text parsers can run on the result,
  3. picks the few pages that feed the Excel table (select_pages), and
  4. provides a Gemini Vision fallback (vision_extract_accounts) used only when
     the OCR-fed rule-based parse fails the report's own summary validation.

Tesseract is the primary (free, local) path; Gemini Vision is the accuracy
safety net. See parser.parse() for the orchestration.
"""

import io
import os
import json

import fitz  # PyMuPDF

# ── Tesseract binary discovery ────────────────────────────────────
# Streamlit Cloud installs it on PATH via packages.txt; on Windows it lands in
# Program Files. Allow an env override (TESSERACT_CMD) for custom installs.
_WIN_CANDIDATES = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]


def _configure_tesseract():
    import pytesseract
    override = os.getenv("TESSERACT_CMD")
    if override and os.path.exists(override):
        pytesseract.pytesseract.tesseract_cmd = override
        return pytesseract
    for cand in _WIN_CANDIDATES:
        if os.path.exists(cand):
            pytesseract.pytesseract.tesseract_cmd = cand
            return pytesseract
    return pytesseract  # assume it's on PATH (Linux / Streamlit Cloud)


# Render scale for OCR (higher = sharper text, slower). 3x ≈ 216 DPI on A4.
_OCR_MATRIX    = fitz.Matrix(3, 3)
# Vision images can be smaller; 2x keeps tokens down while staying legible.
_VISION_MATRIX = fitz.Matrix(2, 2)

_SCAN_TEXT_THRESHOLD = 100  # total stripped chars below this ⇒ treat as scanned


# ─────────────────────────────────────────────────────────────────
# SCAN DETECTION + OCR
# ─────────────────────────────────────────────────────────────────

def is_scanned(doc) -> bool:
    """True when the PDF has effectively no embedded text (image-only scan)."""
    total = 0
    for page in doc:
        total += len(page.get_text().strip())
        if total >= _SCAN_TEXT_THRESHOLD:
            return False
    return True


def _page_image(page, matrix) -> "Image":
    from PIL import Image
    pix = page.get_pixmap(matrix=matrix)
    return Image.open(io.BytesIO(pix.tobytes("png")))


# ── Geometry-aware reconstruction ─────────────────────────────────
# CRIF reports lay each account out as a dense multi-column grid: a label and
# its value share a visual row but sit in different columns. Tesseract's default
# image_to_string reads column-by-column, which ORPHANS values from their labels
# (e.g. "Current Balance:" lands far from "12,34,382"). The text parsers then
# read 0 / NA for those fields.
#
# Instead we pull word boxes (image_to_data) and rebuild reading order ourselves:
# cluster words into visual rows by their y-centre, then order each row left to
# right. This re-unites every "Label: value" pair on one line, which is exactly
# what the inline parser regexes expect.

_ROW_TOL_FRAC = 0.6   # row band tolerance as a fraction of the median word height
_MIN_CONF     = 30    # drop very-low-confidence words (mostly noise glyphs)

# CRIF Commercial ACE prints each account's live/closed status as a vertical
# colored strip in the left margin: "ACTIVE" in red, "CLOSED" in green. This is
# the bureau's own per-account label — far more reliable than guessing from the
# (usually blank) Closure Reason / Closed Date fields. We detect the strip colour
# and inject a marker token into the reconstructed text so the parser can read it.
STATUS_ACTIVE_TOKEN = "__STATUS_ACTIVE__"
STATUS_CLOSED_TOKEN = "__STATUS_CLOSED__"


def _reconstruct_rows(data: dict) -> list:
    """Rebuild visual rows from image_to_data; returns [(row_y, text), ...] by y."""
    import statistics
    words = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except (ValueError, TypeError):
            conf = -1
        if conf < _MIN_CONF:
            continue
        words.append((data["top"][i], data["left"][i], data["height"][i], txt))
    if not words:
        return []

    med_h = statistics.median(w[2] for w in words) or 12
    tol   = med_h * _ROW_TOL_FRAC

    words.sort(key=lambda w: (w[0], w[1]))   # by y, then x
    rows = []   # each: [running_mean_y, count, [(x, text), ...]]
    for top, left, _h, txt in words:
        for row in rows:
            if abs(top - row[0]) <= tol:
                row[0] = (row[0] * row[1] + top) / (row[1] + 1)
                row[1] += 1
                row[2].append((left, txt))
                break
        else:
            rows.append([top, 1, [(left, txt)]])

    rows.sort(key=lambda r: r[0])
    # Single space between words: keeps literal multi-word phrases (e.g. provider
    # markers, "Borrower Summary") intact for substring matching, while \s+ in the
    # parser regexes still tolerates the joins.
    return [(r[0], " ".join(t for _x, t in sorted(r[2]))) for r in rows]


def _detect_status_strips(img) -> list:
    """
    Detect the left-margin status strips. Returns [(y_center_px, token), ...].
    Red strip → ACTIVE, green strip → CLOSED. Coordinates are in the same pixel
    space as image_to_data (both use _OCR_MATRIX), so they map onto the rows.
    """
    try:
        import numpy as np
    except ImportError:
        return []
    arr = np.asarray(img.convert("RGB")).astype(int)
    H, W, _ = arr.shape
    left = arr[:, : int(W * 0.12), :]                 # only the far-left margin
    R, G, B = left[:, :, 0], left[:, :, 1], left[:, :, 2]
    colored  = (left.max(axis=2) - left.min(axis=2)) > 25   # not near-gray
    redish   = colored & (R > G + 15) & (R > B + 15)
    greenish = colored & (G > R + 10) & (G > B + 10)
    rred, rgreen = redish.sum(axis=1), greenish.sum(axis=1)

    marked = [(y, int(rred[y]), int(rgreen[y]))
              for y in range(H) if rred[y] > 3 or rgreen[y] > 3]
    if not marked:
        return []
    # group consecutive marked rows into strips
    strips, cur = [], [marked[0]]
    for rec in marked[1:]:
        if rec[0] - cur[-1][0] <= 5:
            cur.append(rec)
        else:
            strips.append(cur); cur = [rec]
    strips.append(cur)

    out = []
    for s in strips:
        if s[-1][0] - s[0][0] < 30:        # ignore tiny coloured specks
            continue
        rtot = sum(r[1] for r in s); gtot = sum(r[2] for r in s)
        token = STATUS_CLOSED_TOKEN if gtot > rtot else STATUS_ACTIVE_TOKEN
        out.append((s[0][0], s[-1][0], token))   # (y_top, y_bottom, token)
    return out


def _inject_status(rows: list, strips: list) -> None:
    """
    Attach each strip's status token to a text row inside its account block. The
    strip spans the block's field area, so the FIRST row within [y_top, y_bottom]
    is that account's own row (just below its header) — more reliable than mapping
    to the strip centre, which can fall on a header line above the block.
    """
    for y_top, y_bottom, token in strips:
        if not rows:
            return
        inside = [i for i, (ry, _t) in enumerate(rows) if y_top <= ry <= y_bottom]
        idx = inside[0] if inside else min(
            range(len(rows)), key=lambda i: abs(rows[i][0] - (y_top + y_bottom) / 2))
        rows[idx] = (rows[idx][0], rows[idx][1] + " " + token)


def _ocr_page(pytesseract, page) -> str:
    """Geometry-aware OCR of one page, with a plain image_to_string fallback."""
    from pytesseract import Output
    img = _page_image(page, _OCR_MATRIX)
    try:
        data = pytesseract.image_to_data(img, output_type=Output.DICT)
        rows = _reconstruct_rows(data)
        if rows:
            _inject_status(rows, _detect_status_strips(img))
            return "\n".join(t for _y, t in rows)
    except Exception:
        pass
    return pytesseract.image_to_string(img)


def ocr_document(doc, on_progress=None) -> tuple:
    """
    OCR every page. Returns (combined_text, page_texts) where page_texts[i] is
    the geometry-reconstructed OCR of page i (used for page selection). Normalises
    the same unicode quirks parser.extract_text handles.

    on_progress(current, total) is called after each page if provided.
    """
    pytesseract = _configure_tesseract()
    page_texts = []
    total = len(doc)
    for i, page in enumerate(doc):
        page_texts.append(_ocr_page(pytesseract, page))
        if on_progress:
            on_progress(i + 1, total)
    combined = "\n".join(page_texts)
    combined = combined.replace("\xa0", " ").replace("–", "-").replace("—", "-")
    return combined, page_texts


# ─────────────────────────────────────────────────────────────────
# PAGE SELECTION  (only pages that fill the Excel table)
# ─────────────────────────────────────────────────────────────────

_SUMMARY_MARKERS = ("borrower summary", "commercial ace report", "crif high mark score")
_DETAIL_MARKERS  = ("loan terms for", "account trade history")


def select_pages(page_texts: list) -> list:
    """
    Indices of pages worth sending to Vision: the summary page(s) (name, score,
    validation totals) plus every per-account 'Account Trade History' page.
    """
    summary, detail = [], []
    for i, txt in enumerate(page_texts):
        low = txt.lower()
        if any(k in low for k in _DETAIL_MARKERS):
            detail.append(i)
        elif any(k in low for k in _SUMMARY_MARKERS):
            summary.append(i)
    # Keep the first summary page (totals live there); all detail pages.
    chosen = (summary[:1] if summary else [0]) + detail
    return sorted(set(chosen))


# ─────────────────────────────────────────────────────────────────
# GEMINI VISION FALLBACK
# ─────────────────────────────────────────────────────────────────

import base64

_VISION_PROMPT = (
    "These page images are from a CRIF High Mark COMMERCIAL ACE credit report. "
    "Extract EVERY loan account / credit facility shown (each 'Loan Terms For' / "
    "'Account Trade History' block is one account).\n"
    "Return ONLY a valid JSON array, one object per account, with keys:\n"
    "  sr_no, date_of_sanction, sanction_amount, current_balance, emi, overdue, "
    "entity, type_of_loan, max_dpd, status\n"
    "Field rules:\n"
    "- date_of_sanction = the 'Sanctioned Date' field (DD-MM-YYYY). Do NOT use "
    "'Info. as of' or 'Last Payment Date'.\n"
    "- sanction_amount = 'Sanctioned Amount'; current_balance = 'Current Balance' "
    "(these are DIFFERENT fields — read each separately); overdue = 'Amount Overdue'.\n"
    "- entity = the 'Lender' value (use \"Not Disclosed\" if masked as XXXX).\n"
    "- type_of_loan = the 'Type' value.\n"
    "- max_dpd = worst days-past-due in the Payment History grid; 0 if every "
    "entry is Standard/STD.\n"
    "- status = 'Closed' if Closure Reason is WRITTEN OFF / SETTLED / CLOSED or a "
    "Closed Date is present, else 'Active'.\n"
    "Amounts as plain integers in rupees (no commas/symbols). Use 0 when blank or "
    "masked. Do not invent or duplicate accounts."
)


def _img_data_uri(page) -> str:
    pix = page.get_pixmap(matrix=_VISION_MATRIX)
    b64 = base64.b64encode(pix.tobytes("png")).decode()
    return f"data:image/png;base64,{b64}"


def vision_extract_accounts(doc, page_indices: list, api_key: str,
                            invoke_fn=None, postprocess_fn=None,
                            chunk_size: int = 8) -> list:
    """
    Send selected page images to the Gemini model cascade and parse the JSON
    account array. `invoke_fn(api_key, message_content)` and
    `postprocess_fn(raw_str)` are injected by parser.py to reuse its model
    cascade (_llm_invoke) and normalisation (_strip_md/_normalize).

    Pages are processed in chunks: a commercial report can hold 100+ accounts,
    whose JSON would blow past the model's output-token cap in a single call.
    Each chunk's accounts are concatenated; the caller renumbers. Returns [] on
    total failure (individual chunk failures are skipped).
    """
    if not page_indices:
        return []

    all_accounts = []
    for start in range(0, len(page_indices), chunk_size):
        chunk = page_indices[start:start + chunk_size]
        content = [{"type": "text", "text": _VISION_PROMPT}]
        for idx in chunk:
            content.append({"type": "image_url", "image_url": _img_data_uri(doc[idx])})
        try:
            accounts = postprocess_fn(invoke_fn(api_key, content))
            if isinstance(accounts, list):
                all_accounts.extend(accounts)
        except Exception:
            continue
    return all_accounts
