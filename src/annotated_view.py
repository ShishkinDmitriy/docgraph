"""Annotated-HTML viewer — derived (regenerable) artifact for review.

Takes the canonical HTML + the extract graph and produces a self-contained
HTML page where every cited element is annotated with `data-entity` /
`data-types` / `data-label` attributes. Inline JS+CSS highlights covered
elements, color-codes by entity type, and shows a hover tooltip with the
entity URI + types + label.

The annotated view is throwaway — never the source of truth. Always
regenerable via `docgraph view <slug>`. Lives at
`.docgraph/annotated/<slug>.html`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

from src.extract_part14.walker import LIS
from src.html_io import build_class_maps


# ── Entity inventory from the graph ────────────────────────────────────────

def entity_index(graph: Graph) -> dict[str, list[dict]]:
    """Build: HTML fragment → list of entities citing it.

    Each entity record carries:
      uri    — full entity URI as a string
      label  — rdfs:label literal value (or local-name fallback)
      types  — sorted list of rdf:type URIs (string form)

    Multiple entities may cite the same fragment; we keep them all so the
    viewer's tooltip can show a per-element list.
    """
    by_fragment: dict[str, list[dict]] = defaultdict(list)
    # Collect unique entities first, with their types/label
    entities: dict[URIRef, dict] = {}

    def _ensure(uri: URIRef) -> dict:
        rec = entities.get(uri)
        if rec is None:
            label = ""
            for o in graph.objects(uri, RDFS.label):
                label = str(o)
                break
            if not label:
                s = str(uri)
                label = s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
            types = sorted(
                str(t) for t in graph.objects(uri, RDF.type) if isinstance(t, URIRef)
            )
            rec = {"uri": str(uri), "label": label, "types": types}
            entities[uri] = rec
        return rec

    for s, _p, o in graph.triples((None, LIS.representedBy, None)):
        if not isinstance(s, URIRef) or not isinstance(o, URIRef):
            continue
        s_str = str(o)
        if "#" not in s_str:
            continue
        frag = s_str.rsplit("#", 1)[-1].strip()
        if not frag:
            continue
        rec = _ensure(s)
        # Dedupe per fragment (an entity may have multiple representedBy
        # triples for the same fragment — uncommon but possible).
        bucket = by_fragment[frag]
        if not any(e["uri"] == rec["uri"] for e in bucket):
            bucket.append(rec)
    return dict(by_fragment)


# ── HTML annotation pass ───────────────────────────────────────────────────

# Match opening tags with an `id="..."` (or `id='...'`). Backreference for
# the closing quote so apostrophes inside double-quoted values don't break
# capture (same trick as in coverage.py / html_io.py).
_OPEN_TAG_WITH_ID_RX = re.compile(
    r"<\s*([a-zA-Z][a-zA-Z0-9]*)\s+([^>]*?\bid\s*=\s*(['\"])([^'\"]+)\3[^>]*?)>",
    re.DOTALL,
)


def annotate_html(html_text: str, graph: Graph) -> str:
    """Add `data-entity`/`data-label`/`data-types` attributes to every
    element whose `id` (or whose `class-N`) is cited by an entity.

    Returns the modified HTML string. Body content elsewhere is untouched.
    The CSS layer (added by `wrap_annotated_view` below) reads the
    attributes for visual styling and tooltips.
    """
    by_fragment = entity_index(graph)
    _i2c, c2i = build_class_maps(html_text)

    # Reverse lookup: for each id-N, which entities (cite via id-N OR via
    # class-N where the class contains id-N)?
    id_entities: dict[str, list[dict]] = defaultdict(list)
    for frag, entities in by_fragment.items():
        if frag.startswith("id-"):
            for e in entities:
                if not any(x["uri"] == e["uri"] for x in id_entities[frag]):
                    id_entities[frag].append(e)
        elif frag.startswith("class-"):
            for member in c2i.get(frag, ()):
                for e in entities:
                    if not any(x["uri"] == e["uri"] for x in id_entities[member]):
                        id_entities[member].append(e)

    def _replace(m: re.Match) -> str:
        full   = m.group(0)
        id_val = m.group(4)
        entities = id_entities.get(id_val)
        if not entities:
            return full
        # Insert the data-entity attributes just before the closing `>`.
        uris   = " ".join(e["uri"]   for e in entities)
        labels = " | ".join(e["label"] for e in entities)
        types  = " ".join(t for e in entities for t in e["types"])
        extra  = (
            f' data-entity="{_attr_escape(uris)}"'
            f' data-label="{_attr_escape(labels)}"'
            f' data-types="{_attr_escape(types)}"'
        )
        return full[:-1] + extra + ">"

    return _OPEN_TAG_WITH_ID_RX.sub(_replace, html_text)


def _attr_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


# ── Wrapper for the annotated view ─────────────────────────────────────────

_ANNOTATED_CSS = """\
/* Coverage view — overlays the canonical visualization layer with entity-
 * citation highlights. Anything with `data-entity` on it has been cited by
 * the extract graph. */

[data-entity] {
  outline: 2px solid rgba(0, 130, 0, 0.7) !important;
  outline-offset: 2px;
  background: rgba(0, 200, 0, 0.08) !important;
  cursor: help;
  position: relative;
}

[data-entity]::after {
  content: "✓ " attr(data-label);
  font-size: 0.65em;
  background: rgba(0, 130, 0, 0.18);
  color: #060;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 3px;
  vertical-align: super;
  white-space: nowrap;
  font-family: monospace;
}

[data-entity]:hover {
  background: rgba(255, 230, 0, 0.25) !important;
}

/* Tooltip via title attribute is auto-rendered by the browser when present;
 * we set a data-driven title via inline JS at load. */

/* Plain id-only elements (referenceable but not yet cited): pale red,
 * no badge — they show "this could be extracted but wasn't." */
[id]:not([data-entity]) {
  outline: 1px dashed rgba(220, 0, 0, 0.4);
  outline-offset: 1px;
  background: rgba(220, 0, 0, 0.03);
}

/* Sidebar: floating list of all entities with click-to-jump. */
#docgraph-sidebar {
  position: fixed;
  right: 1em;
  top: 1em;
  width: 280px;
  max-height: calc(100vh - 2em);
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.96);
  border: 1px solid #ccc;
  border-radius: 6px;
  padding: 0.7em 0.9em;
  font-size: 0.85em;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  z-index: 1000;
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
}
#docgraph-sidebar h3 {
  margin: 0 0 0.5em 0;
  font-size: 1em;
  color: #333;
}
#docgraph-sidebar .ent { display: block; padding: 2px 0; color: #060;
                        text-decoration: none; cursor: pointer; }
#docgraph-sidebar .ent:hover { background: rgba(255, 230, 0, 0.3); }
#docgraph-sidebar .ent .types { color: #888; font-size: 0.85em; margin-left: 4px; }
"""

_ANNOTATED_JS = """\
(() => {
  // Build a tooltip from data-* attributes; show on hover via title.
  document.querySelectorAll('[data-entity]').forEach(el => {
    const uri    = el.getAttribute('data-entity')    || '';
    const label  = el.getAttribute('data-label')     || '';
    const types  = el.getAttribute('data-types')     || '';
    const lines = [
      'Entity: ' + label,
      'URI:    ' + uri,
      'Types:  ' + (types || '(none)'),
    ];
    el.setAttribute('title', lines.join('\\n'));
  });

  // Sidebar
  const seen = new Map();   // uri → label
  document.querySelectorAll('[data-entity]').forEach(el => {
    const uris   = (el.getAttribute('data-entity') || '').split(/\\s+/);
    const label  = el.getAttribute('data-label') || '';
    const types  = (el.getAttribute('data-types') || '').split(/\\s+/);
    uris.forEach((u, i) => {
      if (!u || seen.has(u)) return;
      seen.set(u, { label: label.split(' | ')[i] || label, types });
    });
  });
  if (seen.size === 0) return;
  const sidebar = document.createElement('div');
  sidebar.id = 'docgraph-sidebar';
  let html = `<h3>Entities (${seen.size})</h3>`;
  for (const [uri, info] of [...seen.entries()].sort((a, b) =>
        a[1].label.localeCompare(b[1].label))) {
    const typeLabels = info.types.map(t => t.split(/[#\\/]/).pop()).join(', ');
    html += `<a class="ent" data-target="${uri}">${info.label}` +
            `<span class="types">${typeLabels}</span></a>`;
  }
  sidebar.innerHTML = html;
  document.body.appendChild(sidebar);

  // Click an entity to scroll to and flash its first mention.
  sidebar.querySelectorAll('.ent').forEach(a => {
    a.addEventListener('click', () => {
      const uri = a.getAttribute('data-target');
      const target = document.querySelector(
        `[data-entity~="${CSS.escape(uri)}"]`);
      if (!target) return;
      target.scrollIntoView({behavior: 'smooth', block: 'center'});
      target.style.transition = 'background 0.4s';
      const orig = target.style.background;
      target.style.background = 'yellow';
      setTimeout(() => { target.style.background = orig; }, 1500);
    });
  });
})();
"""


def wrap_annotated_view(annotated_body: str, *, title: str, lang: str = "und") -> str:
    """Wrap the annotated body in a full HTML doc with overlay CSS+JS.

    `annotated_body` is the canonical body content with `data-entity` etc.
    attrs added by `annotate_html`.
    """
    return (
        "<!DOCTYPE html>\n"
        f"<html lang=\"{lang}\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        f"<title>{_attr_escape(title)} — annotated</title>\n"
        "<style>\n"
        # Pull in the canonical visualization first; annotated view layers on top.
        "body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;\n"
        "       max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; }\n"
        "article { padding: 1em; }\n"
        "h1, h2, h3, h4, h5, h6 { margin-top: 1.2em; }\n"
        "table { border-collapse: collapse; margin: 0.5em 0; }\n"
        "th, td { padding: 4px 8px; border: 1px solid #ddd; text-align: left; }\n"
        "th { background: #f0f0f0; font-weight: 600; }\n"
        f"{_ANNOTATED_CSS}"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"{annotated_body}\n"
        "<script>\n"
        f"{_ANNOTATED_JS}"
        "</script>\n"
        "</body>\n"
        "</html>\n"
    )


# ── File-driven entry point ────────────────────────────────────────────────

def render_annotated_view(html_path: Path, graph_path: Path, *, title: str = "",
                          lang: str = "und") -> str:
    """Read canonical HTML + graph, return annotated-view HTML string."""
    from src.html_io import _strip_outer_html

    full_html = html_path.read_text(encoding="utf-8")
    body      = _strip_outer_html(full_html)

    g = Graph()
    g.parse(str(graph_path), format="turtle")

    annotated_body = annotate_html(body, g)
    page_title     = title or _extract_title(full_html) or html_path.stem
    return wrap_annotated_view(annotated_body, title=page_title, lang=lang)


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    raw = re.sub(r"<[^>]+>", "", m.group(1))
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return cleaned or None
