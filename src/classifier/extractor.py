"""Extract text content from PDF files."""

import base64
import io

import pdfplumber
from pathlib import Path


def extract_text(pdf_path: Path, max_pages: int = 3) -> str:
    """
    Extract text from a PDF, limited to the first few pages.
    For classification we rarely need more than the first 2-3 pages.
    """
    text_parts = []

    with pdfplumber.open(pdf_path) as pdf:
        pages = pdf.pages[:max_pages]
        for page in pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

            # Also extract tables — critical for financial statements
            for table in page.extract_tables():
                for row in table:
                    row_text = "  |  ".join(cell or "" for cell in row)
                    text_parts.append(row_text)

    return "\n".join(text_parts)


def extract_images(pdf_path: Path, max_pages: int = 3) -> list[dict]:
    """
    Convert PDF pages to base64-encoded JPEG images for vision-based classification.
    Used for scanned PDFs that contain no extractable text.
    Saves rendered pages to {pdf_stem}_pages/ next to the PDF for inspection.
    Requires poppler to be installed (apt install poppler-utils / brew install poppler).
    """
    from pdf2image import convert_from_path

    pages_dir = pdf_path.parent / f"{pdf_path.stem}_pages"
    pages_dir.mkdir(exist_ok=True)

    pages = convert_from_path(
        pdf_path, first_page=1, last_page=max_pages, fmt="jpeg", dpi=150
    )
    images = []
    for i, page in enumerate(pages, start=1):
        out_path = pages_dir / f"page_{i}.jpg"
        page.save(out_path, format="JPEG")

        buf = io.BytesIO()
        page.save(buf, format="JPEG")
        images.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(buf.getvalue()).decode(),
                },
                "_path": str(out_path),  # for logging only, stripped before API call
            }
        )
    return images
