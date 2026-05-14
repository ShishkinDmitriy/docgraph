"""Tests for src/annotated_view.py — derived annotated-HTML generation
from canonical HTML + extract graph.

Tests the building blocks (entity index, HTML annotation pass, wrapper)
without exercising the CLI. The CLI is a thin shell.
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from src.annotated_view import annotate_html, entity_index, wrap_annotated_view
from src.extract_part14.walker import LIS


EX = Namespace("http://example.org/x/")


def _graph_with(facts: list[tuple]) -> Graph:
    g = Graph()
    for t in facts:
        g.add(t)
    return g


# ── entity_index ───────────────────────────────────────────────────────────

def test_entity_index_collects_per_fragment_entities():
    g = _graph_with([
        (EX.alice, RDF.type, LIS.Person),
        (EX.alice, RDFS.label, Literal("Alice")),
        (EX.alice, LIS.representedBy, URIRef("http://doc#id-1")),
    ])
    idx = entity_index(g)
    assert "id-1" in idx
    assert idx["id-1"][0]["uri"]   == "http://example.org/x/alice"
    assert idx["id-1"][0]["label"] == "Alice"
    assert "http://rds.posccaesar.org/ontology/lis14/rdl/Person" in idx["id-1"][0]["types"]


def test_entity_index_falls_back_to_local_name_when_no_label():
    g = _graph_with([
        (EX.thing, LIS.representedBy, URIRef("http://doc#id-9")),
    ])
    idx = entity_index(g)
    assert idx["id-9"][0]["label"] == "thing"


def test_entity_index_groups_multiple_entities_at_same_fragment():
    """Two entities both citing the same fragment URI both appear in the
    fragment's bucket."""
    g = _graph_with([
        (EX.a, RDFS.label, Literal("A")),
        (EX.b, RDFS.label, Literal("B")),
        (EX.a, LIS.representedBy, URIRef("http://doc#id-5")),
        (EX.b, LIS.representedBy, URIRef("http://doc#id-5")),
    ])
    idx = entity_index(g)
    labels = sorted(e["label"] for e in idx["id-5"])
    assert labels == ["A", "B"]


def test_entity_index_dedupes_same_entity_with_repeated_triples():
    """Same entity citing the same fragment via multiple representedBy
    triples appears once in the bucket."""
    g = _graph_with([
        (EX.a, RDFS.label, Literal("A")),
        (EX.a, LIS.representedBy, URIRef("http://doc#id-1")),
        (EX.a, LIS.representedBy, URIRef("http://doc#id-1")),
    ])
    idx = entity_index(g)
    assert len(idx["id-1"]) == 1


def test_entity_index_skips_non_uri_objects():
    g = _graph_with([
        (EX.a, LIS.representedBy, Literal("not-a-uri")),
    ])
    assert entity_index(g) == {}


def test_entity_index_skips_uris_without_fragment():
    g = _graph_with([
        (EX.a, LIS.representedBy, URIRef("http://doc")),  # no #
    ])
    assert entity_index(g) == {}


# ── annotate_html ──────────────────────────────────────────────────────────

def test_annotate_adds_data_entity_to_matched_id():
    html = '<p id="id-1">Alice</p><p id="id-2">Bob</p>'
    g = _graph_with([
        (EX.alice, RDFS.label, Literal("Alice")),
        (EX.alice, LIS.representedBy, URIRef("http://doc#id-1")),
    ])
    out = annotate_html(html, g)
    assert 'data-entity="http://example.org/x/alice"' in out
    assert 'data-label="Alice"' in out
    # id-2 has no entity → no data-entity attribute on it
    assert '<p id="id-2">' in out  # unchanged
    assert 'id-2"\n  data-entity' not in out


def test_annotate_class_citation_marks_all_class_members():
    """A `<doc#class-1>` citation marks every element with class-1."""
    html = (
        '<span id="id-1" class="class-1">A1</span>'
        '<span id="id-2" class="class-1">A2</span>'
        '<span id="id-3">B</span>'
    )
    g = _graph_with([
        (EX.a, RDFS.label, Literal("A")),
        (EX.a, LIS.representedBy, URIRef("http://doc#class-1")),
    ])
    out = annotate_html(html, g)
    # Both class-1 members get annotated
    assert out.count('data-entity="http://example.org/x/a"') == 2
    # The unrelated id-3 doesn't
    assert '<span id="id-3">' in out
    assert out.count('data-entity=') == 2


def test_annotate_handles_id_with_apostrophe_in_value():
    """id values with mixed quoting don't trip the regex."""
    # Use single-quoted id to make sure backreference works
    html = "<p id='id-1'>X</p>"
    g = _graph_with([
        (EX.a, RDFS.label, Literal("A")),
        (EX.a, LIS.representedBy, URIRef("http://doc#id-1")),
    ])
    out = annotate_html(html, g)
    assert "data-entity=" in out


def test_annotate_preserves_existing_attributes():
    """Adding data-entity doesn't break other attributes already on the tag."""
    html = '<p id="id-1" class="class-1" data-note="addressee">Alice</p>'
    g = _graph_with([
        (EX.a, RDFS.label, Literal("A")),
        (EX.a, LIS.representedBy, URIRef("http://doc#id-1")),
    ])
    out = annotate_html(html, g)
    assert 'class="class-1"' in out
    assert 'data-note="addressee"' in out
    assert 'data-entity=' in out


def test_annotate_emits_multiple_entities_space_separated():
    """Two entities citing the same id → space-separated URIs in data-entity."""
    html = '<p id="id-1">X</p>'
    g = _graph_with([
        (EX.a, RDFS.label, Literal("A")),
        (EX.b, RDFS.label, Literal("B")),
        (EX.a, LIS.representedBy, URIRef("http://doc#id-1")),
        (EX.b, LIS.representedBy, URIRef("http://doc#id-1")),
    ])
    out = annotate_html(html, g)
    # Both URIs in the value, space-separated
    assert "http://example.org/x/a" in out
    assert "http://example.org/x/b" in out


# ── wrap_annotated_view ────────────────────────────────────────────────────

def test_wrap_includes_lang_attribute():
    out = wrap_annotated_view("<p>x</p>", title="T", lang="de")
    assert '<html lang="de">' in out


def test_wrap_includes_title():
    out = wrap_annotated_view("<p>x</p>", title="My Doc")
    assert "My Doc" in out


def test_wrap_includes_overlay_css_and_js():
    """The wrapper contains the CSS rules + the sidebar-bootstrapping JS."""
    out = wrap_annotated_view("<p>x</p>", title="T")
    assert "[data-entity]" in out
    assert "docgraph-sidebar" in out
