"""
ocr_extractor.py  -  scanned-PDF front-end for AutoCAM.

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
    # We parallelise OCR across pages (one Tesseract call per worker thread), so pin
    # each Tesseract call to a single OpenMP thread  -  otherwise N workers × M internal
    # threads oversubscribe the CPU and run slower.
    os.environ["OMP_THREAD_LIMIT"] = "1"
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
# the bureau's own per-account label  -  far more reliable than guessing from the
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
    is that account's own row (just below its header)  -  more reliable than mapping
    to the strip centre, which can fall on a header line above the block.
    """
    for y_top, y_bottom, token in strips:
        if not rows:
            return
        inside = [i for i, (ry, _t) in enumerate(rows) if y_top <= ry <= y_bottom]
        idx = inside[0] if inside else min(
            range(len(rows)), key=lambda i: abs(rows[i][0] - (y_top + y_bottom) / 2))
        rows[idx] = (rows[idx][0], rows[idx][1] + " " + token)


def _ocr_image(pytesseract, img) -> str:
    """Geometry-aware OCR of one rendered page image, with a plain-text fallback."""
    from pytesseract import Output
    try:
        data = pytesseract.image_to_data(img, output_type=Output.DICT)
        rows = _reconstruct_rows(data)
        if rows:
            _inject_status(rows, _detect_status_strips(img))
            return "\n".join(t for _y, t in rows)
    except Exception:
        pass
    return pytesseract.image_to_string(img)


def _ocr_page(pytesseract, page) -> str:
    """OCR a PyMuPDF page (render, then delegate). Kept for direct callers."""
    return _ocr_image(pytesseract, _page_image(page, _OCR_MATRIX))


# Per-page OCR is ~95% of runtime and pages are independent, so we OCR them in
# parallel. Rendering (MuPDF) stays on the main thread  -  MuPDF documents are NOT
# thread-safe  -  but it runs as a pipeline: the main thread renders page N+1 while the
# worker pool OCRs pages already rendered, so the render cost is hidden behind OCR.
# The GIL is released during the tesseract subprocess, so worker threads truly run in
# parallel. Results are reassembled in page order ⇒ output is byte-identical to serial
# OCR, so accuracy is unchanged by construction.
def _env_int(name: str, default: int) -> int:
    """Positive int from env var `name`, else `default` (deploy-time tuning)."""
    try:
        v = int(os.getenv(name, ""))
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


# Auto-scale to the host's cores, but allow per-deploy overrides:
#   OCR_WORKERS        -  parallel Tesseract workers (lower on a small box to save RAM)
#   OCR_MAX_INFLIGHT   -  rendered-but-not-yet-OCR'd pages held in memory at once
# e.g. on Streamlit Cloud (~1 GB / ~2 cores) set OCR_WORKERS=2, OCR_MAX_INFLIGHT=4.
_OCR_WORKERS      = _env_int("OCR_WORKERS", max(1, min(12, (os.cpu_count() or 4))))
_OCR_MAX_INFLIGHT = _env_int("OCR_MAX_INFLIGHT", _OCR_WORKERS + 4)


def _render_pil(page):
    """Render a page straight to a PIL image from the raw pixmap (no PNG round-trip)."""
    from PIL import Image
    pix  = page.get_pixmap(matrix=_OCR_MATRIX)
    mode = {1: "L", 3: "RGB", 4: "RGBA"}.get(pix.n, "RGB")
    return Image.frombytes(mode, (pix.width, pix.height), pix.samples)


def ocr_document(doc, on_progress=None) -> tuple:
    """
    OCR every page. Returns (combined_text, page_texts) where page_texts[i] is the
    geometry-reconstructed OCR of page i. on_progress(current, total) fires as pages
    complete. Pages render on the main thread and OCR on a worker pool in a bounded
    pipeline; output is identical to serial OCR.
    """
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

    pytesseract = _configure_tesseract()
    total      = len(doc)
    page_texts = [""] * total
    inflight   = {}            # future -> page index
    done       = 0

    def _drain(finished):
        nonlocal done
        for fut in finished:
            page_texts[inflight.pop(fut)] = fut.result()
            done += 1
            if on_progress:
                on_progress(done, total)

    with ThreadPoolExecutor(max_workers=_OCR_WORKERS) as pool:
        for i in range(total):
            if len(inflight) >= _OCR_MAX_INFLIGHT:        # keep memory bounded
                finished, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                _drain(finished)
            img = _render_pil(doc[i])                     # main thread (MuPDF safe)
            inflight[pool.submit(_ocr_image, pytesseract, img)] = i
        _drain(list(inflight))                            # drain the rest

    combined = "\n".join(page_texts)
    combined = combined.replace("\xa0", " ").replace("\u2013", "-").replace("\u2014", "-")
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
    "(these are DIFFERENT fields  -  read each separately); overdue = 'Amount Overdue'.\n"
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


import re as _re


_DPD_PAGE_PROMPT = """\
This page is from a CRIF High Mark COMMERCIAL ACE credit report.
The Payment History/Asset Classification table shows cells like "NNN/xxx" where NNN = days \
past due (e.g. "033/xxx" → 33 DPD, "000/xxx" → 0 DPD, "546/xxx" → 546 DPD). \
Coloured cells (orange/red) mean non-zero DPD — read the number carefully even inside coloured cells.

For EACH account listed below (identified by Sanctioned Date and Sanctioned Amount), \
find its Payment History grid and return the MAXIMUM NNN value across all months.

Accounts on this page:
{account_list}

Return ONLY a JSON object mapping each "DATE|AMOUNT" key to its max DPD integer:
{{"DD-MM-YYYY|AMOUNT": 33, "DD-MM-YYYY|AMOUNT": 546}}
Use 0 if the account's history is all 000 or not found. No other text.\
"""


def _strip_json(text: str) -> str:
    text = _re.sub(r'^```(?:json)?\s*', '', text.strip())
    return _re.sub(r'\s*```$', '', text).strip()


def vision_extract_dpd_from_uri(img_uri: str, accounts: list,
                                api_key: str, invoke_fn) -> dict:
    """
    Ask Gemini to read max DPD for accounts using a pre-rendered page image URI.
    Separated from rendering so callers can parallelise the API calls while keeping
    PyMuPDF rendering on the main thread (MuPDF is not thread-safe).
    Returns {"{date}|{amount}": dpd_int}.
    """
    keys = [f"{a['date_of_sanction']}|{a.get('sanction_amount', 0)}" for a in accounts]
    account_list = "\n".join(f"- {k}" for k in keys)
    content = [
        {"type": "text", "text": _DPD_PAGE_PROMPT.format(account_list=account_list)},
        {"type": "image_url", "image_url": img_uri},
    ]
    try:
        raw    = invoke_fn(api_key, content)
        parsed = json.loads(_strip_json(raw))
        if isinstance(parsed, dict):
            return {k: int(float(str(v))) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


def vision_extract_dpd_per_page(doc, page_idx: int, accounts: list,
                                 api_key: str, invoke_fn) -> dict:
    """Convenience wrapper: renders the page then calls vision_extract_dpd_from_uri."""
    return vision_extract_dpd_from_uri(
        _img_data_uri(doc[page_idx]), accounts, api_key, invoke_fn
    )


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
