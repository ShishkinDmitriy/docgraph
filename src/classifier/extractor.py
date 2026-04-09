"""Extract PDF content for classification."""

import base64
from pathlib import Path


def extract_pdf(pdf_path: Path) -> dict:
    """
    Read a PDF and return an Anthropic document block with prompt caching enabled.
    Claude handles text vs scanned pages internally — no preprocessing needed.
    """
    data = base64.b64encode(pdf_path.read_bytes()).decode()
    return {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": data,
        },
        "cache_control": {"type": "ephemeral"},
    }
