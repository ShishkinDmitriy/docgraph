"""Versioned named-graph deltas — every pipeline step writes a (added,
removed) pair as a TriG file. Materialized state of a scope = compose
the deltas in seq order.

File shape (`.docgraph/graphs/<scope>.<seq:03d>.trig`):

    @prefix dg:   <urn:docgraph:vocab:meta#> .
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


DG = Namespace("urn:docgraph:vocab:meta#")

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
        """Filename component used for delta files.

        Doc scopes use the slug directly (`zahnrechnung2025.NNN.trig`).
        Project scope is the singleton `project.NNN.trig`. RDL scopes
        prefix with `rdl-` (`rdl-posc.NNN.trig`). The reserved-name
        risk for doc slugs colliding with "project" or "rdl-*" is
        unlikely in practice and not guarded — caller should sanitize.
        """
        if self.kind == "doc":
            return _SAFE_RX.sub('-', self.name)
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


def scope_dir(project_root: Path, scope: Scope) -> Path:
    """Per-scope directory under `.docgraph/`:
      - doc:<slug> → `.docgraph/docs/<slug>/`
      - project    → `.docgraph/project/`
      - rdl:<id>   → `.docgraph/rdl/<id>/`

    Every delta + snapshot + (for docs) the canonical html / prompt md
    / annotated viewer file for one scope lives under this directory.
    """
    from src.project import (
        DOCGRAPH_DIR, DOCS_SUBDIR, PROJECT_SCOPE_SUBDIR, RDL_SCOPE_SUBDIR,
    )
    dg = project_root / DOCGRAPH_DIR
    if scope.kind == "doc":
        return dg / DOCS_SUBDIR / scope.name
    if scope.kind == "project":
        return dg / PROJECT_SCOPE_SUBDIR
    if scope.kind == "rdl":
        return dg / RDL_SCOPE_SUBDIR / scope.name
    raise ValueError(f"unknown scope kind: {scope.kind!r}")


def delta_path(project_root: Path, scope: Scope, seq: int) -> Path:
    """`<scope_dir>/delta.<seq:03d>.trig` — the file for one step's delta."""
    return scope_dir(project_root, scope) / f"delta.{seq:03d}.trig"


def list_deltas_for_scope(project_root: Path, scope: Scope) -> list[Path]:
    """Every delta file for `scope`, ordered by seq (filename)."""
    sd = scope_dir(project_root, scope)
    if not sd.is_dir():
        return []
    return sorted(sd.glob("delta.[0-9][0-9][0-9].trig"))


def next_seq(project_root: Path, scope: Scope) -> int:
    """Next available seq for `scope` (1 if no deltas yet)."""
    paths = list_deltas_for_scope(project_root, scope)
    if not paths:
        return 1
    last_seq_str = paths[-1].stem.rsplit(".", 1)[-1]
    return int(last_seq_str) + 1


# ── Namespace propagation ───────────────────────────────────────────────
#
# RULE (apply EVERY time triples cross a serialization boundary): the
# namespace bindings of the source must be copied into the target.
# rdflib's serializers only emit `@prefix` declarations for namespaces
# bound on the target Graph/Dataset — bindings on a "source" Graph that
# the caller iterated triples from are not magically transferred. Files
# missing prefix declarations show ugly fallback `ns1:`, `ns2:` and lose
# the curated readability of the project's vocab.


def copy_namespaces(source, target) -> None:
    """Copy every (prefix, namespace) binding from *source* to *target*.

    Both can be Graph or Dataset. Idempotent — existing bindings on
    target are not overridden. Call this whenever you build a new graph
    by iterating triples from another graph; otherwise the serialized
    file loses every prefix declaration the source had.
    """
    for prefix, ns in source.namespaces():
        try:
            target.bind(prefix, ns, override=False, replace=False)
        except TypeError:
            # Some rdflib Graph.bind variants don't accept replace=False;
            # fall back to the two-arg form.
            target.bind(prefix, ns, override=False)


# ── Write ───────────────────────────────────────────────────────────────


def write_delta(delta: StepDelta, path: Path) -> None:
    """Serialize `delta` to a TriG file at `path`.

    Namespace bindings from the input graphs (delta.added, delta.removed)
    are propagated to the output Dataset BEFORE serialization, so the
    written file's `@prefix` declarations cover every URI the data uses.
    Without this propagation, rdflib emits fallback `ns1:`, `ns2:` …
    instead of the curated `lis:`, `ext:`, `ex:` etc.
    """
    ds = Dataset()
    ds.bind("dg",   DG)
    ds.bind("prov", PROV)
    ds.bind("xsd",  XSD)
    # Carry over the source graphs' bindings (lis, ext, ex, tpl, …)
    copy_namespaces(delta.added,   ds)
    copy_namespaces(delta.removed, ds)

    added_uri   = delta.added_uri
    removed_uri = delta.removed_uri
    scope_uri   = delta.scope.uri
    has_removed = len(delta.removed) > 0

    # Default graph: classification + provenance. The removed graph's
    # metadata is omitted entirely when there are no removals — keeps the
    # file shorter and signals "additions-only" at a glance.
    default_g = ds.default_graph
    _write_graph_meta(default_g, added_uri, DG.GraphAddition,
                      delta, scope_uri)
    if has_removed:
        _write_graph_meta(default_g, removed_uri, DG.GraphRemoval,
                          delta, scope_uri)

    # Named graphs: the actual delta data. Empty removed graph is also
    # not emitted (so the file has just one named-graph block).
    g_added = ds.graph(added_uri)
    for triple in delta.added:
        g_added.add(triple)
    if has_removed:
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
    copy_namespaces(ds, added_g)         # preserve `@prefix` decls from the file
    for t in ds.graph(added_uri):
        added_g.add(t)
    removed_g = Graph()
    copy_namespaces(ds, removed_g)
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
    Namespace bindings are also copied so the snapshot is self-sufficient
    if the caller ever serializes it."""
    out = Graph()
    copy_namespaces(graph, out)
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
    # Propagate bindings from both sides so whichever graph the triple
    # originates from, its prefix is available in the delta files.
    copy_namespaces(before, added_g)
    copy_namespaces(after,  added_g)
    for t in after_set - before_set:
        added_g.add(t)
    removed_g = Graph()
    copy_namespaces(before, removed_g)
    copy_namespaces(after,  removed_g)
    for t in before_set - after_set:
        removed_g.add(t)
    return StepDelta(
        scope=scope, step=step, seq=seq, parent_seq=parent_seq,
        added=added_g, removed=removed_g,
        agent=agent, timestamp=timestamp,
    )


# ── Materialize: compose deltas into the current state ─────────────────


def materialize(project_root: Path, scope: Scope, *, at_seq: int | None = None) -> Graph:
    """Compose the materialized state of `scope` at `at_seq` (or HEAD).

      state(scope, ≤ N) = ⋃ added.i  \\  ⋃ removed.i   for i ≤ N

    Walks delta files in seq order; for each, adds the `added` triples
    and removes the `removed` triples. A removal of a triple not present
    is silently no-op (rdflib Graph.remove behavior).
    """
    out = Graph()
    for path in list_deltas_for_scope(project_root, scope):
        delta = read_delta(path)
        if at_seq is not None and delta.seq > at_seq:
            break
        copy_namespaces(delta.added,   out)
        copy_namespaces(delta.removed, out)
        for triple in delta.added:
            out.add(triple)
        for triple in delta.removed:
            out.remove(triple)
    return out


def list_scopes(project_root: Path) -> list[Scope]:
    """Discover every scope with at least one delta file in the project."""
    from src.project import (
        DOCGRAPH_DIR, DOCS_SUBDIR, PROJECT_SCOPE_SUBDIR, RDL_SCOPE_SUBDIR,
    )
    dg = project_root / DOCGRAPH_DIR
    out: list[Scope] = []

    docs_root = dg / DOCS_SUBDIR
    if docs_root.is_dir():
        for child in sorted(docs_root.iterdir()):
            if child.is_dir() and any(child.glob("delta.[0-9][0-9][0-9].trig")):
                out.append(Scope(kind="doc", name=child.name))

    proj = dg / PROJECT_SCOPE_SUBDIR
    if proj.is_dir() and any(proj.glob("delta.[0-9][0-9][0-9].trig")):
        out.append(Scope(kind="project"))

    rdl_root = dg / RDL_SCOPE_SUBDIR
    if rdl_root.is_dir():
        for child in sorted(rdl_root.iterdir()):
            if child.is_dir() and any(child.glob("delta.[0-9][0-9][0-9].trig")):
                out.append(Scope(kind="rdl", name=child.name))
    return out


def _scope_from_filename_prefix(prefix: str) -> Scope:
    """Reverse of Scope.filename_prefix.

      - `project`        → Scope(kind="project")
      - `rdl-<name>`     → Scope(kind="rdl", name=<name>)
      - anything else    → Scope(kind="doc", name=<prefix>)
                            (doc slugs use the bare prefix; the slug
                            collision with "project" / "rdl-*" is the
                            caller's responsibility to avoid)
    """
    if prefix == "project":
        return Scope(kind="project")
    if prefix.startswith("rdl-"):
        return Scope(kind="rdl", name=prefix[len("rdl-"):])
    return Scope(kind="doc", name=prefix)
