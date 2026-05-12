"""Tests for the shared primitives in `walker.py`.

The per-branch combined walker that used to live in this module has been
replaced by `root_walker.walk_roots`. What remains here are tests for the
small shared utilities used across passes: quote minting and entity URI
slugging.
"""

from __future__ import annotations

from rdflib import Graph, Namespace, URIRef
from rdflib.namespace import RDF

from src.extract_part14.walker import (
    DG,
    LIS,
    OA,
    EvidenceSelector,
    ExtractedEntity,
    mint_entity_uri,
    mint_quote,
)


# ── Quote minting ──────────────────────────────────────────────────────────

def test_mint_quote_emits_oa_textquoteselector():
    g = Graph()
    base_ns = Namespace("http://example.org/source/x/")
    md_uri  = URIRef("http://example.org/source/x/md")

    sel = EvidenceSelector(exact="hello world", prefix="say ", suffix=" today")
    q_uri = mint_quote(g, sel, base_ns=base_ns, md_source_uri=md_uri)

    assert (q_uri, RDF.type, DG.Quote) in g
    assert (q_uri, RDF.type, LIS.InformationObject) in g
    assert (q_uri, OA.hasSource, md_uri) in g

    selectors = list(g.objects(q_uri, OA.hasSelector))
    assert len(selectors) == 1
    sel_node = selectors[0]
    assert (sel_node, RDF.type, OA.TextQuoteSelector) in g
    assert any(g.triples((sel_node, OA.exact, None)))


def test_quote_dedup_same_text():
    """Same exact text → same URI (content-hashed) regardless of prefix/suffix."""
    g = Graph()
    base_ns = Namespace("http://example.org/source/x/")
    md_uri  = URIRef("http://example.org/source/x/md")

    sel1 = EvidenceSelector(exact="identical text")
    sel2 = EvidenceSelector(exact="identical text", prefix="(different prefix)")
    q1 = mint_quote(g, sel1, base_ns=base_ns, md_source_uri=md_uri)
    q2 = mint_quote(g, sel2, base_ns=base_ns, md_source_uri=md_uri)
    assert q1 == q2


def test_quote_different_text_different_uri():
    g = Graph()
    base_ns = Namespace("http://example.org/source/x/")
    md_uri  = URIRef("http://example.org/source/x/md")

    q1 = mint_quote(g, EvidenceSelector(exact="alpha"),
                    base_ns=base_ns, md_source_uri=md_uri)
    q2 = mint_quote(g, EvidenceSelector(exact="beta"),
                    base_ns=base_ns, md_source_uri=md_uri)
    assert q1 != q2


# ── Entity URI minting ─────────────────────────────────────────────────────

def test_mint_entity_uri_uses_single_namespace():
    """Entities mint directly under base_ns — no per-type sub-path. The
    slash-free local name lets rdflib's Turtle serializer use the bound
    `ex:` prefix instead of falling back to the full URI form."""
    base_ns = Namespace("http://example.org/src/")
    u = mint_entity_uri("Dr. Polina Liebermann", base_ns)
    assert str(u) == "http://example.org/src/dr-polina-liebermann"
    # No slash in the local part — that's what lets Turtle use the prefix.
    assert "/" not in str(u).replace(str(base_ns), "")


def test_mint_entity_uri_handles_empty_name():
    """Empty-after-slugging name falls back to a content hash, not an
    empty local name."""
    base_ns = Namespace("http://example.org/src/")
    u = mint_entity_uri("  !!  ", base_ns)
    assert str(u).startswith("http://example.org/src/anon-")


# ── ExtractedEntity multi-typing field ─────────────────────────────────────

def test_extracted_entity_defaults_to_single_typing():
    e = ExtractedEntity(
        uri      = URIRef("http://x/e1"),
        type_uri = LIS.Person,
        label    = "Test",
    )
    assert e.types == []        # legacy single-typed entity
    assert e.type_hints == []


def test_extracted_entity_supports_multi_typing():
    e = ExtractedEntity(
        uri        = URIRef("http://x/e2"),
        type_uri   = LIS.PhysicalObject,
        label      = "Invoice 1352",
        types      = [LIS.PhysicalObject, LIS.InformationObject],
        type_hints = ["DentalInvoice", "MedicalReceipt"],
    )
    assert LIS.PhysicalObject in e.types
    assert LIS.InformationObject in e.types
    assert e.type_hints == ["DentalInvoice", "MedicalReceipt"]
