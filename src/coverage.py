"""Coverage tracking — what fraction of the canonical HTML's atomic units
are cited in the extract graph?

A unit is "covered" when at least one graph triple cites its fragment URI
(either `<doc#id-N>` for the specific element, or `<doc#class-N>` for a
class-N group the element belongs to).

The report has three layers:

  - **Per-id**:  every element with `id="id-N"` is either covered or not.
  - **Per-class**: every `class-N` group is either fully cited (its
    members appear as part of a `<doc#class-N>` triple), partially cited,
    or completely missing.
  - **Per-section** (data-note grouping): coverage within each region the
    LLM tagged with `data-note="..."`.

See docs/architecture/html-pipeline.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from src.extract_part14.walker import LIS
from src.html_io import build_class_maps


# ── Data model ─────────────────────────────────────────────────────────────

@dataclass
class HtmlUnit:
    """An element in the canonical HTML that's a citation target."""
    id_:        str           # the "id-N" string (no '#')
    text:       str           # element's inline text, truncated
    css_class:  str | None    # the "class-N" if present
    section:    str | None    # nearest enclosing data-note value
    tag:        str           # element tag (h1, p, span, td, ...)


@dataclass
class CoverageReport:
    units:           list[HtmlUnit]
    covered_ids:     set[str]            # id-N values cited (directly or via class)
    citations:       set[str]            # raw fragments from the graph: id-N or class-N
    class_to_ids:    dict[str, set[str]]

    @property
    def total(self) -> int:
        return len(self.units)

    @property
    def covered(self) -> int:
        return sum(1 for u in self.units if u.id_ in self.covered_ids)

    @property
    def uncovered(self) -> list[HtmlUnit]:
        return [u for u in self.units if u.id_ not in self.covered_ids]

    @property
    def percent(self) -> float:
        return (self.covered / self.total * 100) if self.total else 0.0


# ── HTML inventory ─────────────────────────────────────────────────────────

# Same lightweight tag tokenizer as html_io. We don't want a hard dep on a
# real HTML parser for what's a well-formed LLM-emitted body. Attribute
# patterns use a backreference for the closing quote so apostrophes inside
# double-quoted values don't truncate the capture; captured value is group 2.
_TAG_RX     = re.compile(r"<\s*(/?)([a-zA-Z][a-zA-Z0-9]*)\s*([^>]*)>", re.DOTALL)
_ID_RX      = re.compile(r"\bid\s*=\s*(['\"])(.*?)\1")
_CLASS_RX   = re.compile(r"\bclass\s*=\s*(['\"])(.*?)\1")
_NOTE_RX    = re.compile(r"\bdata-note\s*=\s*(['\"])(.*?)\1")
_CLASS_N_RX = re.compile(r"\bclass-\d+\b")


def html_inventory(html_text: str, *, text_chars: int = 80) -> list[HtmlUnit]:
    """Walk *html_text* and return every element with an `id="id-N"` as an
    `HtmlUnit`. Captures:
      - id (the "id-N" string)
      - tag (h1 / p / span / td / div / ...)
      - inline text (collapsed whitespace, capped at *text_chars*)
      - class-N if present
      - nearest enclosing data-note (the LLM's section label)
    """
    units: list[HtmlUnit] = []
    # Track the nesting of data-note containers as we walk. Each open tag
    # with a `data-note` attribute pushes; each close pops. The deepest one
    # is the unit's enclosing section.
    note_stack: list[tuple[str, str]] = []   # (tag, note)
    text_buf:   dict[str, list[str]] = {}    # id → accumulating text fragments
    # Stack of currently-open elements that own an id, as (tag, id-N) pairs
    # so we know which id to associate captured text with, and which one to
    # pop when the matching close tag arrives.
    open_ids:   list[tuple[str, str]] = []

    last_pos = 0
    for m in _TAG_RX.finditer(html_text):
        # Capture text between the previous tag and this one — append to
        # every currently-open id-bearing element's buffer.
        if m.start() > last_pos:
            chunk = html_text[last_pos:m.start()]
            for _open_tag, id_val in open_ids:
                text_buf.setdefault(id_val, []).append(chunk)
        last_pos = m.end()

        slash, tag, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""

        if slash == "/":
            # Close — pop note stack and open-ids stack if matching.
            if note_stack and note_stack[-1][0] == tag:
                note_stack.pop()
            if open_ids and open_ids[-1][0] == tag:
                open_ids.pop()
            continue

        id_match    = _ID_RX.search(attrs)
        class_match = _CLASS_RX.search(attrs)
        note_match  = _NOTE_RX.search(attrs)

        if note_match:
            note_stack.append((tag, note_match.group(2)))

        if id_match:
            id_val = id_match.group(2)
            css_cls = None
            if class_match:
                cm = _CLASS_N_RX.search(class_match.group(2))
                if cm:
                    css_cls = cm.group(0)
            section = note_stack[-1][1] if note_stack else None
            units.append(HtmlUnit(
                id_       = id_val,
                text      = "",                # filled in below
                css_class = css_cls,
                section   = section,
                tag       = tag,
            ))
            text_buf[id_val] = []
            # Self-closing tags don't open a scope; everything else does.
            if not attrs.rstrip().endswith("/"):
                open_ids.append((tag, id_val))

    # Realize accumulated text for each unit (collapsed, truncated).
    for u in units:
        raw = "".join(text_buf.get(u.id_, []))
        cleaned = re.sub(r"\s+", " ", raw).strip()
        u.text = (cleaned[:text_chars] + "…") if len(cleaned) > text_chars else cleaned

    return units


# ── Graph inventory ────────────────────────────────────────────────────────

def graph_citations(graph: Graph) -> set[str]:
    """Return the set of fragment strings (e.g., "id-7", "class-2") cited
    via `lis:representedBy` in *graph*.

    Pulls the local fragment (after `#`) from each object URI. Skips
    non-URI objects and URIs without a fragment.
    """
    out: set[str] = set()
    for _s, _p, o in graph.triples((None, LIS.representedBy, None)):
        if not isinstance(o, URIRef):
            continue
        s = str(o)
        if "#" in s:
            frag = s.rsplit("#", 1)[-1].strip()
            if frag:
                out.add(frag)
    return out


# ── Coverage computation ───────────────────────────────────────────────────

def compute_coverage(html_text: str, graph: Graph) -> CoverageReport:
    """Build a CoverageReport for *html_text* against *graph*.

    An id-N element is covered when either:
      1. The graph cites `<doc#id-N>` directly, OR
      2. The graph cites `<doc#class-N>` where N is the class of this
         element (class-level coverage applies to every member).
    """
    units    = html_inventory(html_text)
    _i2c, c2i = build_class_maps(html_text)
    cites    = graph_citations(graph)

    covered_ids: set[str] = set()

    # Direct id-N citations
    for unit in units:
        if unit.id_ in cites:
            covered_ids.add(unit.id_)

    # class-N citations sweep in every member of the class
    for frag in cites:
        if frag in c2i:
            covered_ids.update(c2i[frag])

    return CoverageReport(
        units        = units,
        covered_ids  = covered_ids,
        citations    = cites,
        class_to_ids = c2i,
    )


# ── File-driven entry point ────────────────────────────────────────────────

def coverage_for_files(html_path: Path, graph_path: Path) -> CoverageReport:
    """Load *html_path* + *graph_path* and compute coverage."""
    html_text = html_path.read_text(encoding="utf-8")
    g = Graph()
    g.parse(str(graph_path), format="turtle")
    return compute_coverage(html_text, g)
