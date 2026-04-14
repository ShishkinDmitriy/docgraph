"""Extract PDF content for classification."""

import base64
from pathlib import Path


def extract_pdf(pdf_path: Path) -> dict:
    """
    Read a PDF and return an Anthropic document block with prompt caching enabled.
    Claude handles text vs scanned pages internally — no preprocessing needed.
    """
    raw = pdf_path.read_bytes()
    if not raw:
        raise ValueError(f"{pdf_path}: file is empty")
    if not raw.startswith(b"%PDF"):
        raise ValueError(
            f"{pdf_path}: not a valid PDF (magic bytes missing — got {raw[:8]!r})"
        )
    data = base64.b64encode(raw).decode()
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": data,
        },
        "cache_control": {"type": "ephemeral"},
    }
