"""Generate PlantUML object diagrams from a source's extraction named graph.

Pipeline:  graphs/<slug>.trig  →  diagrams/<slug>.puml  →  diagrams/<slug>.svg

We render PlantUML ourselves from the extraction RDF graph:
    - one ``object "<label>" as <alias> <<Class>>`` per RDF subject
    - ``rdf:type`` values become stereotypes (one per type, joined)
    - datatype properties become members inside the object box
    - object properties become labelled arrows between objects
The .puml is shipped to the public PlantUML server for SVG/PNG rendering.
Best-effort: if the network call fails, the .puml is still on disk.
"""

import urllib.request
import urllib.error
import zlib
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS
from rich.console import Console

from src.ingest import load_combined
from src.project import DOCGRAPH_DIR

DIAGRAMS_SUBDIR = "diagrams"
EXT_NS          = "http://example.org/docgraph/extraction/"
PLANTUML_SERVER = "https://www.plantuml.com/plantuml"

# rdf:type values we don't surface as stereotypes (noise / implicit).
_HIDE_TYPES = {
    "http://www.w3.org/2002/07/owl#NamedIndividual",
    "http://www.w3.org/2002/07/owl#Class",
    "http://www.w3.org/ns/prov#Entity",
}

_DIRECTION_DIRECTIVES = {
    "LR": "left to right direction",
    "TB": "top to bottom direction",
}


class DiagramError(Exception):
    pass


def make_diagram(
    project_root: Path,
    slug: str,
    console: Console,
    *,
    render_format: str = "svg",
    direction: str = "LR",
) -> Path:
    """Generate diagrams/<slug>.puml (and .svg/.png if rendering succeeds).

    Returns the path of the .puml file.
    """
    diagrams_dir = project_root / DOCGRAPH_DIR / DIAGRAMS_SUBDIR
    diagrams_dir.mkdir(exist_ok=True)

    ext_uri = URIRef(f"{EXT_NS}{slug}")
    combined = load_combined(project_root)
    extraction_g = combined.graph(ext_uri)
    if len(extraction_g) == 0:
        raise DiagramError(
            f"extraction graph {ext_uri} is empty or missing — "
            f"is {slug!r} a PDF source that completed classification?"
        )
    console.print(f"  extraction graph: [bold]{len(extraction_g)}[/bold] triple(s)")

    puml_text = _render_object_diagram(extraction_g, slug=slug, direction=direction)

    puml_path = diagrams_dir / f"{slug}.puml"
    puml_path.write_text(puml_text, encoding="utf-8")
    console.print(f"  wrote   [dim]{puml_path.relative_to(project_root)}[/dim] "
                  f"({len(puml_text):,} chars)")

    try:
        rendered = _render_plantuml(puml_text, fmt=render_format)
    except Exception as exc:
        console.print(f"  [yellow]rendering skipped[/yellow]: {exc}")
        return puml_path

    out_path = diagrams_dir / f"{slug}.{render_format}"
    out_path.write_bytes(rendered)
    console.print(f"  rendered [dim]{out_path.relative_to(project_root)}[/dim] "
                  f"({len(rendered):,} bytes)")
    return puml_path


# ─── PlantUML generation ─────────────────────────────────────────────────────

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
        elif p == RDFS.label and isinstance(o, Literal):
            node.label = str(o)
        elif isinstance(o, Literal):
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


# ─── PlantUML server (public) ─────────────────────────────────────────────────

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
