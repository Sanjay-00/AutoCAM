"""
html_extractor.py  -  CRIF/TransUnion HTML report support

These reports are browser-printed to PDF for the digital-PDF flow; when the
raw HTML is available instead, it converts to a text layout close enough to
PyMuPDF's PDF extraction (label on one line, value on the next) that it can
feed the same crif_parser/crif_commercial_parser/tu_parser regex pipeline
unchanged.
"""

import re
import html as _html

_COMMENT      = re.compile(r'(?is)<!--.*?-->')
_SCRIPT_STYLE = re.compile(r'(?is)<(script|style).*?</\1>')
_LINE_BREAK   = re.compile(r'(?is)<(br|/tr|/p|/div|/li|/td|/h[1-6])\s*/?>')
_TAG          = re.compile(r'(?s)<[^>]+>')


def html_to_text(raw: bytes) -> str:
    """Strip an HTML CIBIL report down to plain text, one label/value per line."""
    text = raw.decode("utf-8", errors="replace")
    text = _COMMENT.sub("", text)
    text = _SCRIPT_STYLE.sub("", text)
    text = _LINE_BREAK.sub("\n", text)
    text = _TAG.sub("", text)
    text = _html.unescape(text)
    text = re.sub(r'[ \t]+', ' ', text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r'\n{2,}', '\n', text).strip()
    return text
