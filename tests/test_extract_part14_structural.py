"""Structural extraction (file → document chain) for the part14 pipeline.

Covers the two builders the pipeline writes as separate deltas:
  - build_recognize_graph: file + document typing + dcterms metadata
  - (build_convert_graph is exercised via the full pipeline tests)
"""

from __future__ import annotations

from pathlib import Path

from rdflib import URIRef
from rdflib.namespace import PROV, RDF, RDFS

from src.extract_part14.structural import (
    DG,
    LIS,
    build_recognize_graph,
)


def test_build_recognize_graph_emits_file_doc_typing(tmp_path: Path):
    """Recognize delta types the file as PdfFile + PhysicalObject + prov:Entity
    and the document as Document + InformationObject, linked by
    `<file> lis:represents <doc>` (file bytes embody the abstract work)."""
    file_uri = URIRef("urn:docgraph:source:sample")
    doc_uri  = URIRef("urn:docgraph:source:sample/doc")

    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    g = build_recognize_graph(
        file_path    = pdf_path,
        file_uri     = file_uri,
        doc_uri      = doc_uri,
        project_root = tmp_path,
        file_hash    = "sha256:fake",
        file_size    = 12,
        mime_type    = "application/pdf",
        pdf_info     = {"Pages": "1", "Title": "Sample"},
    )

    # File typed correctly per Part 14
    assert (file_uri, RDF.type, DG.PdfFile)        in g
    assert (file_uri, RDF.type, LIS.PhysicalObject) in g
    assert (file_uri, RDF.type, PROV.Entity)       in g

    # File represents the document (range-free; file is the representor).
    assert (file_uri, LIS.represents, doc_uri) in g
    assert (doc_uri, RDF.type, DG.Document)        in g
    assert (doc_uri, RDF.type, LIS.InformationObject) in g

    # Title from pdfinfo propagates to rdfs:label.
    assert (doc_uri, RDFS.label, None) in [
        (s, p, None) for s, p, o in g.triples((doc_uri, RDFS.label, None))
    ]

    # No chapter / quote artifacts (those are M2's job, top-down).
    assert list(g.subjects(RDF.type, DG.Chapter)) == []
    assert list(g.subjects(RDF.type, DG.Quote))   == []

    # No PROV activity in the recognize delta — conversion activity lives
    # in the convert delta (seq 2), not here.
    assert list(g.subjects(RDF.type, PROV.Activity)) == []


def test_build_recognize_graph_emits_quality_chains_for_metadata(tmp_path: Path):
    """Every file-metadata field becomes a Quality + Datum chain:
    size is scalar (with UoM), path/hash/mime are nominal (no UoM)."""
    file_uri = URIRef("urn:docgraph:source:sample")
    doc_uri  = URIRef("urn:docgraph:source:sample/doc")
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    g = build_recognize_graph(
        file_path    = pdf_path,
        file_uri     = file_uri,
        doc_uri      = doc_uri,
        project_root = tmp_path,
        file_hash    = "sha256:fake",
        file_size    = 12,
        mime_type    = "application/pdf",
        pdf_info     = {"Pages": "1", "Producer": "Adobe Acrobat 9.5"},
    )

    # Scalar size quality — hasPhysicalQuantity chain with dg:Byte UoM.
    size_quality = URIRef("urn:docgraph:source:sample/size")
    size_datum   = URIRef("urn:docgraph:source:sample/size-datum")
    assert (file_uri, LIS.hasPhysicalQuantity, size_quality) in g
    assert (size_quality, RDF.type, DG.FileSize)             in g
    assert (size_quality, LIS.qualityQuantifiedAs, size_datum) in g
    assert (size_datum, RDF.type, LIS.ScalarQuantityDatum)   in g
    assert (size_datum, LIS.datumUOM, DG.Byte)               in g

    # Nominal qualities — hasQuality, no UoM, string datumValue.
    for local, qtype in (("path", DG.FilePath), ("hash", DG.FileHash),
                          ("mime", DG.MimeType), ("createdBy", DG.CreationAgent)):
        quality_uri = URIRef(f"urn:docgraph:source:sample/{local}")
        datum_uri   = URIRef(f"urn:docgraph:source:sample/{local}-datum")
        assert (file_uri, LIS.hasQuality, quality_uri)            in g
        assert (quality_uri, RDF.type, qtype)                     in g
        assert (quality_uri, LIS.qualityQuantifiedAs, datum_uri)  in g
        assert (datum_uri, RDF.type, LIS.QuantityDatum)           in g

    # Page count on the *document* (not the file) — pages are intrinsic
    # to the paginated work, attached via the general lis:hasQuality.
    pages_quality = URIRef("urn:docgraph:source:sample/doc/pages")
    assert (doc_uri, LIS.hasQuality, pages_quality) in g
    assert (pages_quality, RDF.type, DG.PageCount)  in g
