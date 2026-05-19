"""Tests for src/coverage.py — HTML inventory walking + graph-citation
matching + per-section breakdown.

No CLI here; the CLI command (`docgraph coverage`) is a thin shell around
these helpers.
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

from src.coverage import (
    compute_coverage,
    graph_citations,
    html_inventory,
)
from src.extract_part14.walker import LIS


def _graph_with_citations(doc_uri: str, fragments: list[str]) -> Graph:
    """Build a tiny graph asserting `<entity> lis:representedBy <doc#frag>`
    for each fragment in *fragments* (one synthetic entity per fragment)."""
    g = Graph()
    EX = Namespace("http://example.org/x/")
    for i, frag in enumerate(fragments):
        e = EX[f"e{i}"]
        g.add((e, LIS.representedBy, URIRef(f"{doc_uri}#{frag}")))
    return g


# ── html_inventory ─────────────────────────────────────────────────────────

def test_inventory_finds_id_elements():
    html = (
        "<article>"
        '<h1 id="id-1">Title</h1>'
        '<p id="id-2">A paragraph</p>'
        "<p>no id here</p>"
        "</article>"
    )
    units = html_inventory(html)
    ids = [u.id_ for u in units]
    assert ids == ["id-1", "id-2"]


def test_inventory_captures_inline_text():
    html = '<p id="id-1">Hello, world!</p>'
    units = html_inventory(html)
    assert units[0].text == "Hello, world!"


def test_inventory_captures_text_with_nested_span():
    """An id-bearing element's text accumulates across nested children."""
    html = '<p id="id-1">Total: <span>EUR 115.84</span></p>'
    units = html_inventory(html)
    assert units[0].text == "Total: EUR 115.84"


def test_inventory_captures_class_n():
    html = '<span id="id-1" class="class-3 something">X</span>'
    units = html_inventory(html)
    assert units[0].css_class == "class-3"


def test_inventory_class_n_ignores_non_class_n_tokens():
    """class="some-style class-1 also-style" → class-1 captured, others skipped."""
    html = '<span id="id-1" class="big bold class-1 italic">X</span>'
    units = html_inventory(html)
    assert units[0].css_class == "class-1"


def test_inventory_captures_section_from_enclosing_data_note():
    html = (
        '<section data-note="Recipient information">'
        '<p id="id-1">Polina</p>'
        "</section>"
        '<section data-note="Bank details">'
        '<span id="id-2">IBAN123</span>'
        "</section>"
    )
    units = html_inventory(html)
    assert units[0].section == "Recipient information"
    assert units[1].section == "Bank details"


def test_inventory_section_uses_nearest_enclosing_note():
    """An element gets its DEEPEST enclosing data-note (not an outer one)."""
    html = (
        '<section data-note="Outer">'
        '<section data-note="Inner">'
        '<p id="id-1">X</p>'
        "</section>"
        "</section>"
    )
    units = html_inventory(html)
    assert units[0].section == "Inner"


def test_inventory_section_none_when_no_data_note():
    html = '<p id="id-1">X</p>'
    units = html_inventory(html)
    assert units[0].section is None


def test_inventory_captures_tag():
    html = (
        '<h2 id="id-1">a heading</h2>'
        '<td id="id-2">a cell</td>'
        '<span id="id-3">a span</span>'
    )
    units = html_inventory(html)
    assert [u.tag for u in units] == ["h2", "td", "span"]


def test_inventory_truncates_long_text():
    long = "x" * 500
    html = f'<p id="id-1">{long}</p>'
    units = html_inventory(html, text_chars=20)
    assert len(units[0].text) <= 21       # 20 + the ellipsis
    assert units[0].text.endswith("…")


# ── graph_citations ────────────────────────────────────────────────────────

def test_graph_citations_extracts_fragments():
    g = _graph_with_citations("http://doc", ["id-1", "class-2", "id-7"])
    cites = graph_citations(g)
    assert cites == {"id-1", "class-2", "id-7"}


def test_graph_citations_ignores_uris_without_fragment():
    """A bare URI (no #) isn't a citation in our scheme; skip it."""
    g = Graph()
    EX = Namespace("http://example.org/x/")
    g.add((EX.e, LIS.representedBy, URIRef("http://doc")))     # no fragment
    g.add((EX.e, LIS.representedBy, URIRef("http://doc#id-1")))
    assert graph_citations(g) == {"id-1"}


def test_graph_citations_ignores_non_uri_objects():
    """A literal in the representedBy slot is malformed; don't crash."""
    g = Graph()
    EX = Namespace("http://example.org/x/")
    g.add((EX.e, LIS.representedBy, Literal("plain string")))
    assert graph_citations(g) == set()


# ── compute_coverage ───────────────────────────────────────────────────────

def test_coverage_basic_id_match():
    html = '<p id="id-1">X</p><p id="id-2">Y</p>'
    g = _graph_with_citations("http://doc", ["id-1"])
    report = compute_coverage(html, g)
    assert report.total == 2
    assert report.covered == 1
    assert report.covered_ids == {"id-1"}
    assert [u.id_ for u in report.uncovered] == ["id-2"]


def test_coverage_class_n_citation_covers_all_members():
    """One <doc#class-1> citation marks every class-1 member as covered."""
    html = (
        '<p id="id-1" class="class-1">A</p>'
        '<p id="id-2" class="class-1">A again</p>'
        '<p id="id-3" class="class-1">A once more</p>'
        '<p id="id-4">unrelated</p>'
    )
    g = _graph_with_citations("http://doc", ["class-1"])
    report = compute_coverage(html, g)
    assert report.covered_ids == {"id-1", "id-2", "id-3"}
    assert report.covered == 3
    assert [u.id_ for u in report.uncovered] == ["id-4"]


def test_coverage_percent_calculation():
    html = (
        '<p id="id-1">A</p>'
        '<p id="id-2">B</p>'
        '<p id="id-3">C</p>'
        '<p id="id-4">D</p>'
    )
    g = _graph_with_citations("http://doc", ["id-1", "id-2"])
    report = compute_coverage(html, g)
    assert report.percent == 50.0


def test_coverage_no_units_in_html_returns_empty_report():
    html = "<p>no atomic units</p>"
    g = Graph()
    report = compute_coverage(html, g)
    assert report.total == 0
    assert report.covered == 0
    assert report.percent == 0.0


def test_coverage_mixed_class_and_id_citations():
    """The graph cites both `<doc#class-1>` (covers id-1, id-2) and
    `<doc#id-5>` (covers only id-5). Other units uncovered."""
    html = (
        '<p id="id-1" class="class-1">A</p>'
        '<p id="id-2" class="class-1">A2</p>'
        '<p id="id-3">B</p>'
        '<p id="id-4">C</p>'
        '<p id="id-5">D</p>'
    )
    g = _graph_with_citations("http://doc", ["class-1", "id-5"])
    report = compute_coverage(html, g)
    assert report.covered_ids == {"id-1", "id-2", "id-5"}
    assert {u.id_ for u in report.uncovered} == {"id-3", "id-4"}
