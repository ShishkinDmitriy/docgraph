"""Tests for src/html_io.py — primarily the render_markdown_view function
that derives the LLM-input MD view from canonical HTML, with {#id-N}
anchor markers per element that has an `id` attribute.

The render is deliberately minimal — written for the layout-only HTML our
PDF→HTML converter produces (h1-h6, p, ul/ol/li, table/tr/td, blockquote,
span). Whitespace handling is sloppy by design (extra blank lines around
indented HTML are harmless for the LLM's consumption).
"""

from __future__ import annotations

from src.html_io import (
    _wrap_document,
    build_class_maps,
    collapse_anchors,
    render_markdown_view,
)


# ── Element-level markers ──────────────────────────────────────────────────

def test_paragraph_with_id_gets_inline_marker():
    out = render_markdown_view('<p id="id-3">12526 Berlin</p>')
    assert "12526 Berlin {#id-3}" in out


def test_paragraph_without_id_has_no_marker():
    out = render_markdown_view('<p>Plain text</p>')
    assert "{#" not in out
    assert "Plain text" in out


def test_heading_renders_with_hashes_and_marker():
    for level in range(1, 7):
        out = render_markdown_view(f'<h{level} id="id-{level}">Title</h{level}>')
        assert ("#" * level + " Title {#id-" + str(level) + "}") in out


def test_blockquote_renders_with_quote_and_marker():
    out = render_markdown_view('<blockquote id="id-7">Cited text</blockquote>')
    assert "> Cited text {#id-7}" in out


# ── Sub-element span markers ───────────────────────────────────────────────

def test_span_inside_paragraph_gets_marker():
    """An atomic unit wrapped in <span id="..."> appears with its marker
    in the surrounding text, while the surrounding paragraph remains
    unmarked (the span is the addressable unit, not the parent)."""
    out = render_markdown_view('<p>Tel.: <span id="id-13">030 676 61 84</span></p>')
    assert "030 676 61 84 {#id-13}" in out
    # The paragraph itself has no id, so no marker for the paragraph.
    # (One marker total for the span.)
    assert out.count("{#") == 1


def test_multiple_spans_in_one_paragraph_each_get_their_marker():
    out = render_markdown_view(
        '<p>Address: <span id="id-1">street</span>, '
        '<span id="id-2">city</span></p>'
    )
    assert "street {#id-1}" in out
    assert "city {#id-2}" in out


# ── Tables ─────────────────────────────────────────────────────────────────

def test_table_cells_get_per_cell_markers():
    out = render_markdown_view(
        '<table>'
        '<tr><td>Rechnungsnummer</td><td id="id-6">1352</td></tr>'
        '<tr><td>Rechnungsdatum</td><td id="id-7">17.01.2025</td></tr>'
        '</table>'
    )
    assert "| Rechnungsnummer | 1352 {#id-6} |" in out
    assert "| Rechnungsdatum | 17.01.2025 {#id-7} |" in out


def test_table_label_cell_without_id_has_no_marker_only_value_does():
    out = render_markdown_view(
        '<table><tr><td>Label</td><td id="id-X">value</td></tr></table>'
    )
    # Label cell has no marker; value cell does.
    assert "| Label | value {#id-X} |" in out


# ── Lists ──────────────────────────────────────────────────────────────────

def test_unordered_list_items_with_ids():
    out = render_markdown_view(
        '<ul><li id="id-1">first</li><li id="id-2">second</li></ul>'
    )
    assert "- first {#id-1}" in out
    assert "- second {#id-2}" in out


def test_ordered_list_items_get_numbers():
    out = render_markdown_view(
        '<ol><li id="id-1">first</li><li id="id-2">second</li></ol>'
    )
    assert "1. first {#id-1}" in out
    assert "2. second {#id-2}" in out


# ── HTML entities + whitespace handling ────────────────────────────────────

def test_html_entities_are_decoded():
    out = render_markdown_view('<p id="id-1">Tom &amp; Jerry</p>')
    assert "Tom & Jerry {#id-1}" in out


def test_inline_whitespace_collapsed():
    """Multi-line inner text in source HTML collapses to single spaces."""
    out = render_markdown_view(
        '<p id="id-1">line one\n   line two\t\tline three</p>'
    )
    assert "line one line two line three {#id-1}" in out


# ── Overlay divs (stamps, signatures, QR codes) ───────────────────────────

def test_empty_overlay_div_renders_as_overlay_marker():
    out = render_markdown_view(
        '<div id="id-26" data-note="Red PAID stamp"></div>'
    )
    assert "[OVERLAY: Red PAID stamp]" in out
    assert "{#id-26}" in out


def test_overlay_marker_appears_at_position():
    """An overlay div in the middle of a flow appears at that position."""
    out = render_markdown_view(
        '<p>Before</p>'
        '<div id="id-1" data-note="signature"></div>'
        '<p>After</p>'
    )
    pos_before  = out.index("Before")
    pos_overlay = out.index("[OVERLAY: signature]")
    pos_after   = out.index("After")
    assert pos_before < pos_overlay < pos_after


def test_overlay_without_id_renders_marker_without_anchor():
    """An overlay can lack an id (rare — usually we want a citation anchor),
    but the marker still surfaces for diagnostic visibility."""
    out = render_markdown_view('<div data-note="watermark"></div>')
    assert "[OVERLAY: watermark]" in out
    assert "{#" not in out


def test_plain_div_without_data_note_does_not_render_overlay():
    """div without data-note is just a layout container; no OVERLAY marker."""
    out = render_markdown_view('<div><p id="id-1">Content</p></div>')
    assert "OVERLAY" not in out
    assert "Content {#id-1}" in out


# ── Coreference classes ───────────────────────────────────────────────────

def test_marker_includes_class_when_present():
    out = render_markdown_view(
        '<span id="id-4" class="class-1">Little Red Riding Hood</span>'
    )
    assert "Little Red Riding Hood {#id-4 .class-1}" in out


def test_marker_class_only_when_no_id():
    """Coreferent mention can theoretically lack an id (single-mention
    entities). Marker still emits when class is present."""
    out = render_markdown_view('<span class="class-1">named thing</span>')
    assert "named thing {.class-1}" in out


def test_marker_ignores_non_class_n_tokens():
    """class="class-1 some-other-class" → only class-1 surfaces in marker."""
    out = render_markdown_view(
        '<span id="id-4" class="some-other class-1 yet-another">X</span>'
    )
    assert "X {#id-4 .class-1}" in out
    assert "some-other" not in out
    assert "yet-another" not in out


def test_table_cell_class_in_marker():
    out = render_markdown_view(
        '<table><tr><td>Label</td>'
        '<td id="id-2" class="class-1">1352</td></tr></table>'
    )
    assert "| Label | 1352 {#id-2 .class-1} |" in out


# ── Class maps + citation collapse ─────────────────────────────────────────

def test_build_class_maps_finds_id_class_pairs():
    html = (
        '<span id="id-1" class="class-1">A</span>'
        '<span id="id-2" class="class-1">A again</span>'
        '<span id="id-3" class="class-2">B</span>'
    )
    i2c, c2i = build_class_maps(html)
    assert i2c == {"id-1": "class-1", "id-2": "class-1", "id-3": "class-2"}
    assert c2i == {"class-1": {"id-1", "id-2"}, "class-2": {"id-3"}}


def test_build_class_maps_ignores_non_class_n_tokens():
    """class="class-4 some-styling" → only class-4 is tracked; other tokens
    in the class attribute are decorative and irrelevant to coreference."""
    html = '<span id="id-7" class="some-other class-4 also-other">X</span>'
    i2c, _ = build_class_maps(html)
    assert i2c == {"id-7": "class-4"}


def test_build_class_maps_skips_elements_without_both_id_and_class():
    html = (
        '<p id="id-1">just id</p>'
        '<span class="class-1">just class</span>'
        '<span id="id-2" class="class-1">both</span>'
    )
    i2c, c2i = build_class_maps(html)
    assert i2c == {"id-2": "class-1"}
    assert c2i == {"class-1": {"id-2"}}


def test_collapse_full_coverage_emits_class_fragment():
    """All members of class-1 cited → emit one class fragment."""
    i2c = {"id-1": "class-1", "id-2": "class-1", "id-3": "class-1"}
    c2i = {"class-1": {"id-1", "id-2", "id-3"}}
    out = collapse_anchors({"id-1", "id-2", "id-3"}, i2c, c2i)
    assert out == ["class-1"]


def test_collapse_partial_coverage_falls_back_to_ids():
    """Class has 3 members; only 2 cited → enumerate per-id (don't fib
    coverage with a class fragment that includes uncited members)."""
    i2c = {"id-1": "class-1", "id-2": "class-1", "id-3": "class-1"}
    c2i = {"class-1": {"id-1", "id-2", "id-3"}}
    out = collapse_anchors({"id-1", "id-2"}, i2c, c2i)
    assert out == ["id-1", "id-2"]


def test_collapse_mixed_classes_some_covered_some_not():
    i2c = {
        "id-1": "class-1", "id-2": "class-1",       # class-1 fully cited
        "id-3": "class-2", "id-4": "class-2",       # class-2 partial (id-4 missing)
        "id-9": None,                                # ignored
    }
    c2i = {
        "class-1": {"id-1", "id-2"},
        "class-2": {"id-3", "id-4", "id-9b"},
    }
    out = collapse_anchors({"id-1", "id-2", "id-3"}, i2c, c2i)
    # class-1 fully covered → collapse. class-2 partial → enumerate.
    assert "class-1" in out
    assert "id-3" in out
    assert "class-2" not in out


def test_collapse_unclassed_ids_passthrough():
    """Ids that aren't in any class group pass through as per-id fragments."""
    i2c = {}
    c2i = {}
    out = collapse_anchors({"id-7", "id-11"}, i2c, c2i)
    assert out == ["id-11", "id-7"]


def test_collapse_empty_input():
    assert collapse_anchors(set(), {}, {}) == []


# ── Document wrapper (DOCTYPE + lang + visualization CSS) ─────────────────

def test_wrapper_uses_doc_lang_attribute():
    out = _wrap_document({"title": "Rechnung", "lang": "de", "html": "<article></article>"})
    assert '<html lang="de">' in out


def test_wrapper_defaults_lang_to_und_when_missing():
    """When the LLM doesn't supply a lang, fall back to 'und' (undetermined,
    BCP-47 standard for unknown language) rather than guessing English."""
    out = _wrap_document({"title": "X", "html": "<p>X</p>"})
    assert '<html lang="und">' in out


def test_wrapper_handles_empty_lang_string():
    out = _wrap_document({"title": "X", "lang": "", "html": "<p>X</p>"})
    assert '<html lang="und">' in out


def test_wrapper_html_escapes_lang_attribute():
    """A pathological lang value with quotes shouldn't break attribute syntax."""
    out = _wrap_document({"title": "X", "lang": 'evil"hack', "html": "<p>X</p>"})
    assert 'evil"hack' not in out
    assert "&quot;" in out


# ── End-to-end shape ───────────────────────────────────────────────────────

def test_full_document_shape():
    """A miniature invoice-shaped document round-trips with all expected
    markers in the right positions."""
    html = (
        '<article>'
        '<header><h1 id="id-1">Acme Co</h1>'
        '<p>Address: <span id="id-2">10 Main St</span></p></header>'
        '<p id="id-3">Customer: Bob</p>'
        '<table><tr><td>Order#</td><td id="id-4">42</td></tr></table>'
        '<p id="id-5">Total: EUR 99.50</p>'
        '</article>'
    )
    md = render_markdown_view(html)

    # Each id appears once, in expected position
    for n in (1, 2, 3, 4, 5):
        assert f"{{#id-{n}}}" in md, f"missing marker for id-{n}"

    # Heading rendering
    assert "# Acme Co {#id-1}" in md
    # Span marker stays inline with surrounding paragraph text
    assert "10 Main St {#id-2}" in md
    # Table cell carries its marker
    assert "| Order# | 42 {#id-4} |" in md
