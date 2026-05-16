"""Versioned named-graph deltas — every pipeline step writes a (added,
removed) pair as a TriG file. Materialized state of a scope = compose
the deltas in seq order.

File shape (`.docgraph/graphs/<scope>.<seq:03d>.trig`):

    @prefix dg:   <http://example.org/docgraph/meta#> .
    @prefix prov: <http://www.w3.org/ns/prov#> .
    @prefix xsd:  <http://www.w3.org/2001/XMLSchema#> .

    # Default graph: classification + provenance about the two named graphs
    <urn:docgraph:delta/<scope>/001/added>
        a dg:GraphAddition ;
        dg:scope     <urn:docgraph:scope/<scope>> ;
        dg:step      "extract" ;
        dg:seq       1 ;
        dg:parentSeq 0 ;
        prov:wasGeneratedBy <agent-uri> ;
        prov:atTime         "..."^^xsd:dateTime .

    <urn:docgraph:delta/<scope>/001/removed>
        a dg:GraphRemoval ;
        dg:scope <urn:docgraph:scope/<scope>> ;
        dg:step "extract" ; dg:seq 1 ; dg:parentSeq 0 .

    # Named graph: triples added
    <urn:docgraph:delta/<scope>/001/added> { … }

    # Named graph: triples removed (often empty)
    <urn:docgraph:delta/<scope>/001/removed> { … }

Three scopes today: `doc:<slug>` for per-document work, `project` for
project-wide writes (e.g., promoted ext: classes), `rdl:<id>` for cached
remote RDL data. Per-scope monotonic seq.

This module is the foundational plumbing — it doesn't yet wire into the
pipeline. Phase 2 migrates the convert step to return a StepDelta; later
phases migrate extract / templates / dedup, then the loader prefers
deltas over the existing `.ttl` snapshots.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import PROV, RDF, XSD

logger = logging.getLogger(__name__)


DG = Namespace("http://example.org/docgraph/meta#")

# URI namespaces for delta named graphs and scope identifiers.
DELTA_NS = "urn:docgraph:delta/"
SCOPE_NS = "urn:docgraph:scope/"

# Filename-safe scope component. Slugs from `make_slug` are already safe;
# this guards against any other producer.
_SAFE_RX = re.compile(r"[^A-Za-z0-9._-]+")


# ── Scope ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Scope:
    """A graph-write target. Three kinds today:

      doc:<slug>   — per-document work (entities, properties, templates)
      project      — project-wide ontology / promoted classes / cross-doc
      rdl:<id>     — cached remote RDL data
    """
    kind: str             # "doc" | "project" | "rdl"
    name: str = ""        # identifier within the kind ("" for project, the singleton)

    @property
    def filename_prefix(self) -> str:
        """Filename component, e.g. `doc-zahnrechnung2025`, `project`,
        `rdl-posc`. Kept identifier-safe; underscores and dots are kept,
        anything else collapses to `-`."""
        if self.name:
            return f"{self.kind}-{_SAFE_RX.sub('-', self.name)}"
        return self.kind

    @property
    def uri(self) -> URIRef:
        """Stable URI for this scope, used as the value of dg:scope on
        every delta in this scope."""
        if self.name:
            return URIRef(f"{SCOPE_NS}{self.kind}/{self.name}")
        return URIRef(f"{SCOPE_NS}{self.kind}")


def doc_scope(slug: str) -> Scope:    return Scope(kind="doc",     name=slug)
def project_scope() -> Scope:         return Scope(kind="project")
def rdl_scope(rdl_id: str) -> Scope:  return Scope(kind="rdl",     name=rdl_id)


# ── StepDelta ────────────────────────────────────────────────────────────


@dataclass
class StepDelta:
    """One pipeline step's contribution to one scope.

    A step can emit MULTIPLE deltas in a single run when it touches
    multiple scopes — e.g., dedup writes a doc-scope delta (the
    substitutions and audits in this doc's graph) AND a project-scope
    delta (promoted ext class definitions).

    `parent_seq` records the seq immediately before this delta's seq.
    For seq=1, parent_seq is 0. The chain is informational; composition
    walks all deltas with seq ≤ N regardless.
    """
    scope:      Scope
    step:       str               # "convert" | "extract" | "templates" | "dedup" | …
    seq:        int               # monotonic per-scope, 1-based
    added:      Graph             # triples added by this step
    removed:    Graph = field(default_factory=Graph)
    parent_seq: int = 0
    agent:      URIRef | None = None
    timestamp:  datetime | None = None

    @property
    def added_uri(self) -> URIRef:
        return URIRef(f"{DELTA_NS}{self.scope.filename_prefix}/{self.seq:03d}/added")

    @property
    def removed_uri(self) -> URIRef:
        return URIRef(f"{DELTA_NS}{self.scope.filename_prefix}/{self.seq:03d}/removed")


# ── File path helpers ───────────────────────────────────────────────────


def delta_path(graphs_dir: Path, scope: Scope, seq: int) -> Path:
    """Path for `<scope-prefix>.<seq:03d>.trig` under graphs_dir."""
    return graphs_dir / f"{scope.filename_prefix}.{seq:03d}.trig"


def list_deltas_for_scope(graphs_dir: Path, scope: Scope) -> list[Path]:
    """Every delta file for `scope`, ordered by seq (filename)."""
    pattern = f"{scope.filename_prefix}.[0-9][0-9][0-9].trig"
    return sorted(graphs_dir.glob(pattern))


def next_seq(graphs_dir: Path, scope: Scope) -> int:
    """Next available seq for `scope` (1 if no deltas yet)."""
    paths = list_deltas_for_scope(graphs_dir, scope)
    if not paths:
        return 1
    last_seq_str = paths[-1].stem.rsplit(".", 1)[-1]
    return int(last_seq_str) + 1


# ── Write ───────────────────────────────────────────────────────────────


def write_delta(delta: StepDelta, path: Path) -> None:
    """Serialize `delta` to a TriG file at `path`."""
    ds = Dataset()
    ds.bind("dg",   DG)
    ds.bind("prov", PROV)
    ds.bind("xsd",  XSD)

    added_uri   = delta.added_uri
    removed_uri = delta.removed_uri
    scope_uri   = delta.scope.uri

    # Default graph: per-graph metadata (classification + provenance).
    default_g = ds.default_graph
    _write_graph_meta(default_g, added_uri,   DG.GraphAddition,
                      delta, scope_uri)
    _write_graph_meta(default_g, removed_uri, DG.GraphRemoval,
                      delta, scope_uri)

    # Named graphs: the actual delta data.
    g_added = ds.graph(added_uri)
    for triple in delta.added:
        g_added.add(triple)
    g_removed = ds.graph(removed_uri)
    for triple in delta.removed:
        g_removed.add(triple)

    path.parent.mkdir(parents=True, exist_ok=True)
    ds.serialize(destination=str(path), format="trig")


def _write_graph_meta(default_g: Graph, graph_uri: URIRef, kind: URIRef,
                      delta: StepDelta, scope_uri: URIRef) -> None:
    default_g.add((graph_uri, RDF.type,        kind))
    default_g.add((graph_uri, DG.scope,        scope_uri))
    default_g.add((graph_uri, DG.step,         Literal(delta.step)))
    default_g.add((graph_uri, DG.seq,          Literal(delta.seq, datatype=XSD.integer)))
    default_g.add((graph_uri, DG.parentSeq,    Literal(delta.parent_seq, datatype=XSD.integer)))
    if delta.agent is not None:
        default_g.add((graph_uri, PROV.wasGeneratedBy, delta.agent))
    if delta.timestamp is not None:
        default_g.add((graph_uri, PROV.atTime,
                       Literal(delta.timestamp.isoformat(), datatype=XSD.dateTime)))


# ── Read ────────────────────────────────────────────────────────────────


def read_delta(path: Path) -> StepDelta:
    """Parse a delta file at `path`. Raises ValueError on malformed input."""
    ds = Dataset()
    ds.parse(path, format="trig")

    default_g = ds.default_graph
    added_uri = next(
        (s for s in default_g.subjects(RDF.type, DG.GraphAddition) if isinstance(s, URIRef)),
        None,
    )
    if added_uri is None:
        raise ValueError(f"{path}: no dg:GraphAddition in default graph")
    removed_uri = next(
        (s for s in default_g.subjects(RDF.type, DG.GraphRemoval) if isinstance(s, URIRef)),
        None,
    )

    scope = _scope_from_uri(_first_object(default_g, added_uri, DG.scope))
    step  = str(_first_object(default_g, added_uri, DG.step) or "")
    seq   = _int_object(default_g, added_uri, DG.seq, default=0)
    parent_seq = _int_object(default_g, added_uri, DG.parentSeq, default=0)

    added_g = Graph()
    for t in ds.graph(added_uri):
        added_g.add(t)
    removed_g = Graph()
    if removed_uri is not None:
        for t in ds.graph(removed_uri):
            removed_g.add(t)

    agent = _first_object(default_g, added_uri, PROV.wasGeneratedBy)
    if agent is not None and not isinstance(agent, URIRef):
        agent = None

    ts_lit = _first_object(default_g, added_uri, PROV.atTime)
    timestamp = None
    if isinstance(ts_lit, Literal):
        try:
            timestamp = datetime.fromisoformat(str(ts_lit))
        except ValueError:
            pass

    return StepDelta(
        scope=scope, step=step, seq=seq,
        added=added_g, removed=removed_g, parent_seq=parent_seq,
        agent=agent, timestamp=timestamp,
    )


def _first_object(graph: Graph, subject: URIRef, predicate: URIRef):
    return next(graph.objects(subject, predicate), None)


def _int_object(graph: Graph, subject: URIRef, predicate: URIRef, *, default: int = 0) -> int:
    obj = _first_object(graph, subject, predicate)
    if isinstance(obj, Literal):
        try:
            return int(obj.toPython())
        except (TypeError, ValueError):
            pass
    return default


def _scope_from_uri(uri) -> Scope:
    """Reverse of Scope.uri — parse `urn:docgraph:scope/<kind>[/<name>]`."""
    if not isinstance(uri, URIRef):
        return Scope(kind="unknown")
    s = str(uri)
    if not s.startswith(SCOPE_NS):
        return Scope(kind="unknown")
    rest = s[len(SCOPE_NS):]
    parts = rest.split("/", 1)
    kind = parts[0]
    name = parts[1] if len(parts) > 1 else ""
    return Scope(kind=kind, name=name)


# ── Diff helpers ────────────────────────────────────────────────────────


def snapshot(graph: Graph) -> Graph:
    """Return a fresh copy of `graph` — for before/after delta computation.

    rdflib Graphs are mutable and don't support a cheap "freeze". We make
    a shallow copy of the triple set so the caller can keep a reference
    to the pre-step state, mutate the original, then compute the diff.
    Bindings are not preserved (we only need the triples for diff)."""
    out = Graph()
    for triple in graph:
        out.add(triple)
    return out


def delta_from_diff(
    before:     Graph,
    after:      Graph,
    *,
    scope:      Scope,
    step:       str,
    seq:        int,
    parent_seq: int = 0,
    agent:      URIRef | None = None,
    timestamp:  datetime | None = None,
) -> StepDelta:
    """Build a StepDelta from before/after triple sets via set difference.

    `added`   = triples in *after*  but not in *before*
    `removed` = triples in *before* but not in *after*
    """
    before_set = set(before)
    after_set  = set(after)
    added_g = Graph()
    for t in after_set - before_set:
        added_g.add(t)
    removed_g = Graph()
    for t in before_set - after_set:
        removed_g.add(t)
    return StepDelta(
        scope=scope, step=step, seq=seq, parent_seq=parent_seq,
        added=added_g, removed=removed_g,
        agent=agent, timestamp=timestamp,
    )


# ── Materialize: compose deltas into the current state ─────────────────


def materialize(graphs_dir: Path, scope: Scope, *, at_seq: int | None = None) -> Graph:
    """Compose the materialized state of `scope` at `at_seq` (or HEAD).

      state(scope, ≤ N) = ⋃ added.i  \\  ⋃ removed.i   for i ≤ N

    Walks delta files in seq order; for each, adds the `added` triples
    and removes the `removed` triples. A removal of a triple not present
    is silently no-op (rdflib Graph.remove behavior).
    """
    out = Graph()
    for path in list_deltas_for_scope(graphs_dir, scope):
        delta = read_delta(path)
        if at_seq is not None and delta.seq > at_seq:
            break
        for triple in delta.added:
            out.add(triple)
        for triple in delta.removed:
            out.remove(triple)
    return out


def list_scopes(graphs_dir: Path) -> list[Scope]:
    """Discover every scope with at least one delta file under graphs_dir."""
    seen: dict[str, Scope] = {}
    for path in graphs_dir.glob("*.[0-9][0-9][0-9].trig"):
        # Filename like `doc-zahnrechnung2025.001.trig` → prefix = `doc-zahnrechnung2025`
        prefix = path.stem.rsplit(".", 1)[0]
        if prefix in seen:
            continue
        seen[prefix] = _scope_from_filename_prefix(prefix)
    return sorted(seen.values(), key=lambda s: s.filename_prefix)


def _scope_from_filename_prefix(prefix: str) -> Scope:
    """Reverse of Scope.filename_prefix — parse `<kind>` or `<kind>-<name>`.

    Note: kind is "doc" / "project" / "rdl" (no dashes), so the first `-`
    cleanly separates kind from name.
    """
    if "-" in prefix:
        kind, name = prefix.split("-", 1)
        return Scope(kind=kind, name=name)
    return Scope(kind=prefix)
