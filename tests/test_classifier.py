"""Basic tests for the classifier (no API calls)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from src.classifier import ClassificationResult
from src.organizer import organize


def test_organize_creates_correct_path(tmp_path):
    pdf = tmp_path / "statement.pdf"
    pdf.write_bytes(b"%PDF fake content")

    result = ClassificationResult(
        category="1099-INT",
        confidence=0.95,
        reason="Shows interest income box",
        tax_year="2024",
    )

    dest = organize(pdf, result, output_dir=tmp_path / "out", dry_run=True)

    assert dest == tmp_path / "out" / "2024" / "1099-INT" / "statement.pdf"


def test_organize_handles_collision(tmp_path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF fake")

    result = ClassificationResult(
        category="W-2",
        confidence=0.9,
        reason="Employer wage statement",
        tax_year="2024",
    )

    out = tmp_path / "out"
    # First copy — creates the file
    dest1 = organize(pdf, result, output_dir=out)
    # Second copy — should get a suffix
    dest2 = organize(pdf, result, output_dir=out)

    assert dest1 != dest2
    assert dest2.stem == "doc_1"
