"""Tests for src/deltas.py — versioned-graph delta plumbing.

Covers Scope/StepDelta dataclasses, file-naming, write/read round-trip,
materialize composition (apply deltas in seq order), and scope discovery.
No pipeline wiring is exercised here (Phase 2 introduces that).
"""

from __future__ import annotations

from datetime import datetime, timezone

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from src.deltas import (
    DG,
    Scope,
    StepDelta,
    delta_from_diff,
    delta_path,
    doc_scope,
    list_deltas_for_scope,
    list_scopes,
    materialize,
    next_seq,
    project_scope,
    rdl_scope,
    read_delta,
    snapshot,
    write_delta,
)


EX  = Namespace("http://example.org/test/")
LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")


# ── Scope ──────────────────────────────────────────────────────────────────

def test_doc_scope_filename_prefix_and_uri():
    s = doc_scope("zahnrechnung2025")
    assert s.filename_prefix == "zahnrechnung2025"
    assert str(s.uri) == "urn:docgraph:scope/doc/zahnrechnung2025"


def test_project_scope_singleton_no_name():
    s = project_scope()
    assert s.filename_prefix == "project"
    assert str(s.uri) == "urn:docgraph:scope/project"


def test_rdl_scope():
    s = rdl_scope("posc-caesar")
    assert s.filename_prefix == "rdl-posc-caesar"
    assert str(s.uri) == "urn:docgraph:scope/rdl/posc-caesar"


def test_scope_is_hashable():
    """Frozen dataclass — usable as dict key, set member."""
    a = doc_scope("foo")
    b = doc_scope("foo")
    assert {a, b} == {a}        # equality + hashability collapse them


# ── File path helpers ─────────────────────────────────────────────────────

def test_delta_path_format(tmp_path):
    p = delta_path(tmp_path, doc_scope("zahnrechnung2025"), 7)
    assert p.name == "zahnrechnung2025.007.trig"
    assert p.parent == tmp_path


def test_next_seq_starts_at_one(tmp_path):
    assert next_seq(tmp_path, doc_scope("foo")) == 1


def test_next_seq_increments_after_writes(tmp_path):
    s = doc_scope("foo")
    g_added = Graph(); g_added.add((EX.x, RDF.type, EX.Y))
    write_delta(StepDelta(scope=s, step="convert", seq=1, added=g_added), delta_path(tmp_path, s, 1))
    assert next_seq(tmp_path, s) == 2
    write_delta(StepDelta(scope=s, step="extract", seq=2, added=g_added,
                          parent_seq=1), delta_path(tmp_path, s, 2))
    assert next_seq(tmp_path, s) == 3


# ── Round-trip ────────────────────────────────────────────────────────────

def test_write_then_read_round_trip(tmp_path):
    """A serialized delta reads back to an equivalent StepDelta."""
    g_added = Graph()
    g_added.add((EX.acme,    RDF.type,    LIS.Organization))
    g_added.add((EX.acme,    RDFS.label,  Literal("Acme Corp")))
    g_removed = Graph()
    g_removed.add((EX.stale, RDF.type,    EX.Junk))

    s     = doc_scope("acme-doc")
    agent = URIRef("http://example.org/agent/walk-mega")
    when  = datetime(2026, 5, 16, 12, 34, 56, tzinfo=timezone.utc)
    delta = StepDelta(scope=s, step="extract", seq=2, added=g_added,
                      removed=g_removed, parent_seq=1,
                      agent=agent, timestamp=when)
    path = delta_path(tmp_path, s, delta.seq)
    write_delta(delta, path)
    assert path.is_file()

    loaded = read_delta(path)
    assert loaded.scope.kind == "doc"
    assert loaded.scope.name == "acme-doc"
    assert loaded.step       == "extract"
    assert loaded.seq        == 2
    assert loaded.parent_seq == 1
    assert loaded.agent      == agent
    assert loaded.timestamp  == when
    # Triple sets match
    assert set(loaded.added)   == set(g_added)
    assert set(loaded.removed) == set(g_removed)


def test_round_trip_preserves_namespace_bindings(tmp_path):
    """REGRESSION: source graphs' prefix bindings (lis, ext, ex, …)
    must survive write→read round-trip. Without explicit propagation
    in write_delta, the TriG serializer falls back to ns1:, ns2:, …
    losing the curated readability of the project's vocab."""
    g_added = Graph()
    g_added.bind("lis", LIS, override=True)
    g_added.bind("ex",  EX,  override=True)
    g_added.add((EX.acme, RDF.type, LIS.Organization))
    g_added.add((EX.acme, RDFS.label, Literal("Acme")))

    s = doc_scope("acme")
    delta = StepDelta(scope=s, step="extract", seq=1, added=g_added)
    path = delta_path(tmp_path, s, 1)
    write_delta(delta, path)

    # File content should contain explicit @prefix declarations for
    # both lis and ex, not fallback ns1:/ns2: aliases.
    text = path.read_text()
    assert "@prefix lis:" in text
    assert "@prefix ex:"  in text
    assert "@prefix ns"   not in text     # no fallback alias

    # Reading back should also surface them on the parsed Graph.
    loaded = read_delta(path)
    loaded_prefixes = {pfx for pfx, _ in loaded.added.namespaces()}
    assert "lis" in loaded_prefixes
    assert "ex"  in loaded_prefixes


def test_round_trip_with_empty_removed(tmp_path):
    """Most steps have no removals — empty removed graph is still valid."""
    g_added = Graph()
    g_added.add((EX.foo, RDF.type, EX.Bar))
    s = doc_scope("foo")
    delta = StepDelta(scope=s, step="convert", seq=1, added=g_added)
    path = delta_path(tmp_path, s, 1)
    write_delta(delta, path)

    loaded = read_delta(path)
    assert set(loaded.added)   == set(g_added)
    assert len(loaded.removed) == 0


def test_classification_triples_in_default_graph(tmp_path):
    """Reading the file as a flat Turtle (ignoring named graphs) should
    surface dg:GraphAddition for the added graph. dg:GraphRemoval is
    only emitted when there ARE removals (so files for additions-only
    steps stay compact)."""
    s = doc_scope("foo")
    g_added = Graph(); g_added.add((EX.x, RDF.type, EX.Y))
    delta = StepDelta(scope=s, step="extract", seq=1, added=g_added)
    path = delta_path(tmp_path, s, 1)
    write_delta(delta, path)

    from rdflib import Dataset
    ds = Dataset()
    ds.parse(path, format="trig")
    default_g = ds.default_graph
    additions = list(default_g.subjects(RDF.type, DG.GraphAddition))
    removals  = list(default_g.subjects(RDF.type, DG.GraphRemoval))
    assert len(additions) == 1
    assert len(removals)  == 0       # additions-only step → no removal block
    scope_uri = doc_scope("foo").uri
    assert (additions[0], DG.scope, scope_uri) in default_g


def test_removal_metadata_emitted_only_when_non_empty(tmp_path):
    """Companion regression to the above: when the delta DOES have
    removals, the dg:GraphRemoval metadata + named-graph block both land."""
    s = doc_scope("bar")
    g_added = Graph(); g_added.add((EX.x, RDF.type, EX.Y))
    g_removed = Graph(); g_removed.add((EX.gone, RDF.type, EX.Y))
    write_delta(
        StepDelta(scope=s, step="dedup", seq=2, added=g_added,
                  removed=g_removed, parent_seq=1),
        delta_path(tmp_path, s, 2),
    )
    from rdflib import Dataset
    ds = Dataset()
    ds.parse(delta_path(tmp_path, s, 2), format="trig")
    removals = list(ds.default_graph.subjects(RDF.type, DG.GraphRemoval))
    assert len(removals) == 1


# ── Materialize composition ──────────────────────────────────────────────

def test_materialize_empty_when_no_deltas(tmp_path):
    g = materialize(tmp_path, doc_scope("nothing"))
    assert len(g) == 0


def test_materialize_single_delta_returns_added(tmp_path):
    s = doc_scope("foo")
    g_added = Graph()
    g_added.add((EX.x, RDFS.label, Literal("X")))
    write_delta(StepDelta(scope=s, step="convert", seq=1, added=g_added),
                delta_path(tmp_path, s, 1))

    g = materialize(tmp_path, s)
    assert (EX.x, RDFS.label, Literal("X")) in g


def test_materialize_composes_add_then_remove(tmp_path):
    """seq 1 adds X; seq 2 removes X. Materialized state has no X."""
    s = doc_scope("foo")
    g_add = Graph(); g_add.add((EX.x, RDF.type, EX.Y))
    write_delta(StepDelta(scope=s, step="convert", seq=1, added=g_add),
                delta_path(tmp_path, s, 1))
    g_rm = Graph(); g_rm.add((EX.x, RDF.type, EX.Y))
    write_delta(StepDelta(scope=s, step="dedup", seq=2, added=Graph(),
                          removed=g_rm, parent_seq=1),
                delta_path(tmp_path, s, 2))

    final = materialize(tmp_path, s)
    assert len(final) == 0


def test_materialize_composes_remove_then_re_add(tmp_path):
    """seq 1 adds X; seq 2 removes X; seq 3 re-adds X. Materialized
    state has X (re-add wins because it comes later in seq order)."""
    s = doc_scope("foo")
    g_x = Graph(); g_x.add((EX.x, RDF.type, EX.Y))
    for seq, added, removed in [(1, g_x,    Graph()),
                                  (2, Graph(), g_x),
                                  (3, g_x,    Graph())]:
        write_delta(StepDelta(scope=s, step="step", seq=seq, added=added,
                              removed=removed, parent_seq=seq - 1),
                    delta_path(tmp_path, s, seq))

    final = materialize(tmp_path, s)
    assert (EX.x, RDF.type, EX.Y) in final


def test_materialize_at_seq_returns_historical_state(tmp_path):
    """Materializing `at_seq=1` should ignore later deltas — historical view."""
    s = doc_scope("foo")
    g_x = Graph(); g_x.add((EX.x, RDF.type, EX.Y))
    write_delta(StepDelta(scope=s, step="convert", seq=1, added=g_x),
                delta_path(tmp_path, s, 1))
    g_rm = Graph(); g_rm.add((EX.x, RDF.type, EX.Y))
    write_delta(StepDelta(scope=s, step="dedup", seq=2, added=Graph(),
                          removed=g_rm, parent_seq=1),
                delta_path(tmp_path, s, 2))

    historical = materialize(tmp_path, s, at_seq=1)
    assert (EX.x, RDF.type, EX.Y) in historical
    current = materialize(tmp_path, s)
    assert len(current) == 0


# ── Scope discovery ──────────────────────────────────────────────────────

def test_list_scopes_finds_distinct_scopes(tmp_path):
    g = Graph(); g.add((EX.x, RDF.type, EX.Y))
    for scope in (doc_scope("a"), doc_scope("b"), project_scope(),
                  rdl_scope("posc")):
        write_delta(StepDelta(scope=scope, step="convert", seq=1, added=g),
                    delta_path(tmp_path, scope, 1))

    scopes = list_scopes(tmp_path)
    prefixes = {s.filename_prefix for s in scopes}
    assert {"a", "b", "project", "rdl-posc"} == prefixes


def test_list_deltas_for_scope_orders_by_seq(tmp_path):
    s = doc_scope("foo")
    g = Graph(); g.add((EX.x, RDF.type, EX.Y))
    # Write seqs out of order to confirm sort, not write order
    for seq in (3, 1, 2):
        write_delta(StepDelta(scope=s, step="step", seq=seq, added=g,
                              parent_seq=max(0, seq - 1)),
                    delta_path(tmp_path, s, seq))

    paths = list_deltas_for_scope(tmp_path, s)
    seqs = [int(p.stem.rsplit(".", 1)[-1]) for p in paths]
    assert seqs == [1, 2, 3]


# ── Multi-scope independence ─────────────────────────────────────────────

# ── snapshot + delta_from_diff (Phase 3 helpers) ─────────────────────────

def test_snapshot_copies_triples_not_reference():
    """snapshot() returns a new Graph; mutating the original doesn't
    affect the snapshot."""
    g = Graph()
    g.add((EX.x, RDF.type, EX.Y))
    snap = snapshot(g)
    g.add((EX.z, RDF.type, EX.Y))
    assert (EX.z, RDF.type, EX.Y) in g
    assert (EX.z, RDF.type, EX.Y) not in snap


def test_delta_from_diff_captures_added_and_removed():
    """delta_from_diff computes added=after−before, removed=before−after."""
    before = Graph()
    before.add((EX.x, RDF.type, EX.Y))
    before.add((EX.gone, RDF.type, EX.Y))    # will be removed
    after = Graph()
    after.add((EX.x, RDF.type, EX.Y))         # unchanged
    after.add((EX.new, RDF.type, EX.Y))       # added

    d = delta_from_diff(
        before, after,
        scope=doc_scope("foo"), step="dedup", seq=2, parent_seq=1,
    )
    assert set(d.added)   == {(EX.new,  RDF.type, EX.Y)}
    assert set(d.removed) == {(EX.gone, RDF.type, EX.Y)}


def test_delta_from_diff_empty_when_no_change():
    """If before == after, the resulting delta has empty added/removed
    sets (so the caller can choose to skip writing it)."""
    g = Graph(); g.add((EX.x, RDF.type, EX.Y))
    d = delta_from_diff(
        snapshot(g), g,
        scope=doc_scope("foo"), step="noop", seq=1,
    )
    assert len(d.added)   == 0
    assert len(d.removed) == 0


def test_seqs_are_per_scope(tmp_path):
    """Doc A's seqs don't affect doc B's."""
    a, b = doc_scope("a"), doc_scope("b")
    g = Graph(); g.add((EX.x, RDF.type, EX.Y))
    for seq in (1, 2, 3):
        write_delta(StepDelta(scope=a, step="s", seq=seq, added=g,
                              parent_seq=max(0, seq - 1)),
                    delta_path(tmp_path, a, seq))
    assert next_seq(tmp_path, a) == 4
    assert next_seq(tmp_path, b) == 1   # B has nothing yet
