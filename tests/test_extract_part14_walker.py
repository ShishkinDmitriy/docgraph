"""Tests for the shared primitives in `walker.py`.

The per-branch combined walker that used to live in this module has been
replaced by `root_walker.walk_roots`. The `dg:Quote + oa:hasSelector`
clusters that quote-minting used to produce have been replaced by direct
fragment URIs into the canonical HTML (`<doc#id-N>`); see
`docs/architecture/html-pipeline.md`.

What remains here are tests for the small shared utilities used across
passes: fragment URI minting, entity URI slugging, and the EvidenceSelector
dataclass.
"""

from __future__ import annotations

from rdflib import Namespace, URIRef

from src.extract_part14.walker import (
    DG,
    LIS,
    OA,
    EvidenceSelector,
    ExtractedEntity,
    mint_entity_uri,
    mint_fragment_uri,
)


# ── Fragment URIs (citations into canonical HTML) ──────────────────────────

def test_mint_fragment_uri_appends_anchor():
    doc = URIRef("http://example.org/source/x/invoice.html")
    u = mint_fragment_uri(doc, "id-7")
    assert str(u) == "http://example.org/source/x/invoice.html#id-7"


def test_mint_fragment_uri_strips_leading_hash():
    """LLM may emit "#id-7" or "id-7"; both should resolve to the same URI."""
    doc = URIRef("http://example.org/source/x/invoice.html")
    u1 = mint_fragment_uri(doc, "id-7")
    u2 = mint_fragment_uri(doc, "#id-7")
    assert u1 == u2


def test_mint_fragment_uri_strips_whitespace():
    doc = URIRef("http://example.org/source/x/invoice.html")
    u = mint_fragment_uri(doc, "  id-7  ")
    assert str(u).endswith("#id-7")


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


# ── EvidenceSelector ───────────────────────────────────────────────────────

def test_evidence_selector_carries_text_and_anchor():
    sel = EvidenceSelector(exact="EUR 115.84", anchor="id-9")
    assert sel.exact == "EUR 115.84"
    assert sel.anchor == "id-9"


def test_evidence_selector_anchor_optional():
    """The anchor is optional in the dataclass; the walker enforces it
    being present at minting time."""
    sel = EvidenceSelector(exact="hello")
    assert sel.anchor == ""


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
