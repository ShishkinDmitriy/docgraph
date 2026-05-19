"""diagram — render docs/<slug>/diagram.{puml,svg,png} from graph.ttl.

Pipeline:
  graph[.NNN].ttl  →  diagram[.NNN].puml  →  diagram[.NNN].svg

Reads the doc-scope Turtle snapshot produced by the `snapshot` task and
renders PlantUML:
    - one ``object "<label>" as <alias> <<Class>>`` per RDF subject
    - ``rdf:type`` values become stereotypes (one per type, joined)
    - datatype properties become members inside the object box
    - object properties become labelled arrows between objects
The .puml is shipped to the public PlantUML server for SVG/PNG rendering.
Best-effort: if the network call fails, the .puml is still on disk.

ctx contract:
    project_root  — required
    slug          — required
    console       — required
    render_format — optional ("svg" default; "png" supported)
    direction     — optional ("LR" default; "TB" supported)
    at_seq        — optional (None = HEAD; otherwise the historical seq)

Dirty check: clean iff the target diagram file exists AND its mtime is
≥ graph[.NNN].ttl's mtime. File-system check, not graph-content check —
diagrams are rendered artifacts, not part of the RDF model.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
import zlib
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import PROV, RDF, RDFS
from rich.console import Console

from src.project import diagram_path, graph_ttl_path
from src.tasks._registry import docgraph


# ── Task definition ─────────────────────────────────────────────────────

class DiagramError(Exception):
    pass


@docgraph.task("diagram", deps=("snapshot",))
def diagram(ctx) -> None:
    console = ctx["console"]
    try:
        _make_diagram(
            ctx["project_root"], ctx["slug"], console,
            render_format = ctx.get("render_format", "svg"),
            direction     = ctx.get("direction", "LR"),
            at_seq        = ctx.get("at_seq"),
        )
    except DiagramError as exc:
        console.print(f"  [yellow]diagram skipped[/yellow]: {exc}")
    except Exception as exc:
        console.print(f"  [yellow]diagram failed[/yellow]: {exc}")


@docgraph.dirty("diagram")
def diagram_dirty(ctx) -> bool:
    return not _diagram_is_current(
        ctx["project_root"], ctx["slug"],
        at_seq = ctx.get("at_seq"),
        fmt    = ctx.get("render_format", "svg"),
    )


# ── Rendering core ──────────────────────────────────────────────────────

PLANTUML_SERVER    = "https://www.plantuml.com/plantuml"
LIS_NS             = "http://rds.posccaesar.org/ontology/lis14/rdl/"
LIS_REPRESENTED_BY = URIRef(LIS_NS + "representedBy")

# rdf:type values we don't surface as stereotypes (noise / implicit).
_HIDE_TYPES = {
    "http://www.w3.org/2002/07/owl#NamedIndividual",
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/ns/prov#Entity",
}

# Predicate-namespace filter — anything under these is dropped before
# rendering (too noisy for a logical object diagram). rdfs:label is
# special-cased earlier (it becomes the object's display label).
_HIDE_PREDICATE_NAMESPACES = (
    str(RDFS),                                   # rdfs:comment, rdfs:subClassOf, …
    str(PROV),                                   # prov:wasGeneratedBy, prov:used, …
)

# `lis:representedBy <doc#id-N>` / `<doc#class-N>` triples are pure
# HTML-anchor citations — they say *where* the entity was seen, not
# anything about the entity itself. We drop those but keep representedBy
# edges that point at a real entity (no `#id-N` / `#class-N` fragment).
_ANCHOR_FRAGMENT_RX = re.compile(r"#(?:id|class)-\d+$")

_DIRECTION_DIRECTIVES = {
    "LR": "left to right direction",
    "TB": "top to bottom direction",
}


def _diagram_is_current(project_root: Path, slug: str, *,
                         at_seq: int | None = None,
                         fmt: str = "svg") -> bool:
    """True iff the diagram file for *slug* exists AND its mtime is at
    least as new as graph[.NNN].ttl. The snapshot task is responsible for
    keeping graph.ttl fresh; we just compare timestamps against it."""
    target = diagram_path(project_root, slug, fmt=fmt, at_seq=at_seq)
    if not target.exists():
        return False
    snap = graph_ttl_path(project_root, slug, at_seq=at_seq)
    if not snap.exists():
        return True                                   # nothing to be stale against
    return target.stat().st_mtime >= snap.stat().st_mtime


def _make_diagram(
    project_root: Path,
    slug: str,
    console: Console,
    *,
    render_format: str = "svg",
    direction: str = "LR",
    at_seq: int | None = None,
) -> Path:
    """Generate `docs/<slug>/diagram.puml` (+ .svg/.png if rendering succeeds)
    by parsing the doc-scope Turtle snapshot.

    With ``at_seq=None`` writes the HEAD diagram (`diagram.*`). With a seq,
    writes a historical snapshot (`diagram.NNN.*`), parallel to `graph.NNN.ttl`.

    Returns the path of the .puml file.
    """
    snap = graph_ttl_path(project_root, slug, at_seq=at_seq)
    if not snap.exists():
        at_label = f" at seq={at_seq}" if at_seq is not None else ""
        raise DiagramError(
            f"{snap.name} for {slug!r}{at_label} not found — "
            f"run `docgraph snapshot {slug}` first."
        )

    extraction_g = Graph()
    extraction_g.parse(snap, format="turtle")
    if len(extraction_g) == 0:
        at_label = f" at seq={at_seq}" if at_seq is not None else ""
        raise DiagramError(
            f"{snap.name} for {slug!r}{at_label} is empty."
        )
    console.print(f"  extraction graph: [bold]{len(extraction_g)}[/bold] triple(s)")

    puml_text = _render_object_diagram(extraction_g, slug=slug, direction=direction)

    puml_path = diagram_path(project_root, slug, fmt="puml", at_seq=at_seq)
    puml_path.write_text(puml_text, encoding="utf-8")
    console.print(f"  wrote   [dim]{puml_path.name}[/dim] "
                  f"({len(puml_text):,} chars)")

    try:
        rendered = _render_plantuml(puml_text, fmt=render_format)
    except Exception as exc:
        console.print(f"  [yellow]rendering skipped[/yellow]: {exc}")
        return puml_path

    out_path = diagram_path(project_root, slug, fmt=render_format, at_seq=at_seq)
    out_path.write_bytes(rendered)
    console.print(f"  rendered [dim]{out_path.name}[/dim] "
                  f"({len(rendered):,} bytes)")
    return puml_path


# ── PlantUML generation ──────────────────────────────────────────────────

class _Node:
    __slots__ = ("uri", "types", "literals", "label")

    def __init__(self, uri: URIRef):
        self.uri = uri
        self.types: list[URIRef] = []
        self.literals: list[tuple[URIRef, Literal]] = []
        self.label: str | None = None


def _render_object_diagram(g: Graph, *, slug: str, direction: str) -> str:
    nm = g.namespace_manager
    nodes: dict[URIRef, _Node] = {}
    edges: list[tuple[URIRef, URIRef, URIRef]] = []

    for s, p, o in g:
        if not isinstance(s, URIRef):
            continue
        node = nodes.setdefault(s, _Node(s))

        if p == RDF.type:
            if isinstance(o, URIRef) and str(o) not in _HIDE_TYPES:
                node.types.append(o)
            continue
        if p == RDFS.label and isinstance(o, Literal):
            node.label = str(o)
            continue
        if _is_hidden_predicate(p, o):
            continue
        if isinstance(o, Literal):
            node.literals.append((p, o))
        elif isinstance(o, URIRef):
            nodes.setdefault(o, _Node(o))
            edges.append((s, p, o))

    lines: list[str] = [f"@startuml {slug}"]
    if directive := _DIRECTION_DIRECTIVES.get(direction.upper()):
        lines.append(directive)
    lines.append("")

    aliases = {uri: f"o{i}" for i, uri in enumerate(nodes, 1)}

    for uri, node in nodes.items():
        alias = aliases[uri]
        label = node.label or _qname(uri, nm)
        stereotypes = "".join(f" <<{_qname(t, nm)}>>" for t in _dedup(node.types))
        head = f'object "{_quote(label)}" as {alias}{stereotypes}'
        if node.literals:
            lines.append(f"{head} {{")
            for prop, value in node.literals:
                lines.append(f"  {_qname(prop, nm)} = {_format_literal(value)}")
            lines.append("}")
        else:
            lines.append(head)

    if edges:
        lines.append("")
    for s, p, o in edges:
        lines.append(f"{aliases[s]} --> {aliases[o]} : {_qname(p, nm)}")

    lines.append("@enduml")
    return "\n".join(lines)


def _is_hidden_predicate(p: URIRef, o) -> bool:
    """True if a (p, o) triple is noise that should be dropped from the diagram.

    - Anything under the rdfs:/prov:/ namespaces (except rdfs:label, which
      the caller has already special-cased).
    - `lis:representedBy` when the object is just an HTML-anchor citation
      (`<doc#id-N>` / `<doc#class-N>`) — these clutter the diagram with one
      edge per cited element. A representedBy pointing at a real entity URI
      (no `#id-N` / `#class-N` fragment) survives.
    """
    s = str(p)
    if s.startswith(_HIDE_PREDICATE_NAMESPACES):
        return True
    if p == LIS_REPRESENTED_BY and isinstance(o, URIRef):
        return bool(_ANCHOR_FRAGMENT_RX.search(str(o)))
    return False


def _qname(uri: URIRef, nm) -> str:
    """Render *uri* as `prefix:local` using only registered namespaces;
    fall back to the local-name tail when no prefix matches."""
    s = str(uri)
    longest = ("", "")
    for prefix, ns in nm.namespaces():
        ns_str = str(ns)
        if s.startswith(ns_str) and len(ns_str) > len(longest[1]):
            longest = (prefix, ns_str)
    if longest[1]:
        local = s[len(longest[1]):]
        return f"{longest[0]}:{local}" if longest[0] and local else (local or s)
    return _local_name(uri)


def _dedup(items: list[URIRef]) -> list[URIRef]:
    seen: set[URIRef] = set()
    out: list[URIRef] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _local_name(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s or str(uri)


def _quote(s: str) -> str:
    return s.replace('\\', r'\\').replace('"', r'\"')


def _format_literal(lit: Literal) -> str:
    val = str(lit).replace("\n", " ")
    if len(val) > 80:
        val = val[:77] + "..."
    return '"' + _quote(val) + '"'


# ── PlantUML server (public) ────────────────────────────────────────────

_PUML_TABLE = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_"


def _render_plantuml(puml_text: str, *, fmt: str = "svg", timeout: float = 30.0) -> bytes:
    encoded = _plantuml_encode(puml_text)
    url = f"{PLANTUML_SERVER}/{fmt}/{encoded}"
    req = urllib.request.Request(url, headers={"User-Agent": "docgraph/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise DiagramError(f"PlantUML server HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise DiagramError(f"PlantUML server unreachable: {exc.reason}") from exc


def _plantuml_encode(text: str) -> str:
    """Deflate (raw, no zlib wrapper) + PlantUML's custom 64-char alphabet."""
    deflated = zlib.compress(text.encode("utf-8"))[2:-4]
    out: list[str] = []
    n = len(deflated)
    i = 0
    while i < n:
        b1 = deflated[i]
        b2 = deflated[i + 1] if i + 1 < n else 0
        b3 = deflated[i + 2] if i + 2 < n else 0
        out.append(_PUML_TABLE[b1 >> 2])
        out.append(_PUML_TABLE[((b1 & 0x3) << 4) | (b2 >> 4)])
        out.append(_PUML_TABLE[((b2 & 0xF) << 2) | (b3 >> 6)])
        out.append(_PUML_TABLE[b3 & 0x3F])
        i += 3
    return "".join(out)
