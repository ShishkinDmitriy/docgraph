"""Structural extraction (file → document chain) for the part14 pipeline.

M1 strips chapters and quotes — they're now M2's responsibility (top-down,
evidence-driven). This file tests:
  - parse_markdown helper (still used by M2, kept here)
  - structural-only filter (--, bare headings)
  - build_chain emits file → doc in Part 14 idiom
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from rdflib import URIRef
from rdflib.namespace import PROV, RDF, RDFS

from src.extract_part14.structural import (
    DG,
    LIS,
    build_chain,
    parse_markdown,
)


# ── parse_markdown — kept as helper for M2's structured-context input ────────

def test_parse_markdown_splits_chapters_at_h2():
    md = (
        "## Introduction\n"
        "First paragraph.\n"
        "\n"
        "Second paragraph.\n"
        "\n"
        "## Methods\n"
        "Methods paragraph.\n"
    )
    chapters = parse_markdown(md)
    assert [c.title for c in chapters] == ["Introduction", "Methods"]
    assert len(chapters[0].quotes) == 2
    assert len(chapters[1].quotes) == 1


def test_parse_markdown_filters_horizontal_rules():
    md = "## Section\nReal paragraph.\n\n---\n\nAnother paragraph.\n"
    chapters = parse_markdown(md)
    quote_texts = [q.text for q in chapters[0].quotes]
    assert "Real paragraph." in quote_texts
    assert "Another paragraph." in quote_texts
    assert "---" not in quote_texts        # horizontal rule filtered
    assert len(chapters[0].quotes) == 2    # nothing else slipped through


def test_parse_markdown_filters_bare_headings():
    md = "## Section\n# Standalone heading\n\nReal content here.\n"
    chapters = parse_markdown(md)
    quote_texts = [q.text for q in chapters[0].quotes]
    assert "# Standalone heading" not in quote_texts
    assert "Real content here." in quote_texts


def test_parse_markdown_locator_format():
    md = "## Chap\none\n\ntwo\n"
    chapters = parse_markdown(md)
    locators = [q.locator for q in chapters[0].quotes]
    assert locators == ["Chap / ¶1", "Chap / ¶2"]


def test_quote_uris_are_content_hashes():
    md = "## A\nIdentical text.\n"
    chapters = parse_markdown(md)
    md2 = "## B\nIdentical text.\n"
    chapters2 = parse_markdown(md2)
    assert chapters[0].quotes[0].uri_local == chapters2[0].quotes[0].uri_local


# ── build_chain — M1's actual output ────────────────────────────────────────

def test_build_chain_emits_file_doc_only(tmp_path: Path):
    file_uri = URIRef("http://example.org/source/sample")
    doc_uri  = URIRef("http://example.org/source/sample/doc")

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    now = datetime.now(timezone.utc)
    g = build_chain(
        file_path            = pdf_path,
        file_uri             = file_uri,
        doc_uri              = doc_uri,
        document_title       = "Sample",
        document_description = "An invoice",
        project_root         = tmp_path,
        file_hash            = "sha256:fake",
        file_size            = 12,
        mime_type            = "application/pdf",
        pdf_info             = {"Pages": "1", "Title": "Sample"},
        convert_started      = now,
        convert_ended        = now,
    )

    # File typed correctly per Part 14
    assert (file_uri, RDF.type, DG.PdfFile) in g
    assert (file_uri, RDF.type, LIS.PhysicalObject) in g
    assert (file_uri, RDF.type, PROV.Entity) in g

    # File represents the document
    assert (file_uri, LIS.representedBy, doc_uri) in g
    assert (doc_uri, RDF.type, DG.Document) in g
    assert (doc_uri, RDF.type, LIS.InformationObject) in g
    assert (doc_uri, RDFS.label, None) in [(s, p, None) for s, p, o in g.triples((doc_uri, RDFS.label, None))]

    # NO chapters in the graph (M1 strips them)
    chapters = list(g.subjects(RDF.type, DG.Chapter))
    assert chapters == []

    # NO quotes in the graph (M1 strips them; M2 mints top-down)
    quotes = list(g.subjects(RDF.type, DG.Quote))
    assert quotes == []

    # PROV-O activity for conversion
    activities = list(g.subjects(RDF.type, PROV.Activity))
    assert len(activities) == 1
    assert (activities[0], PROV.used, file_uri) in g
    assert (activities[0], PROV.generated, doc_uri) in g
