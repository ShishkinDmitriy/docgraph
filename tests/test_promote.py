"""Tests for the ext-class promotion phase.

walk_promote() scans per-doc graphs for ext: class declarations,
counts contributing docs, and promotes classes meeting the threshold
into project-scope. Each contributing doc gets a removal delta.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from src.deltas import (
    StepDelta,
    delta_path,
    doc_scope,
    list_deltas_for_scope,
    materialize,
    project_scope,
    write_delta,
)
from src.extract_part14.ext_ontology import (
    EXT,
    LIS,
    DG,
    ExtClass,
    class_definitions_graph,
    extract_classes_from_graph,
)
from src.extract_part14.promote import walk_promote


def _seed_doc_with_ext_class(tmp_path: Path, slug: str, cls: ExtClass,
                              seq: int = 1) -> None:
    """Helper: write a doc-scope seq-N delta that declares one ext class."""
    g = Graph()
    for t in class_definitions_graph([cls]):
        g.add(t)
    write_delta(
        StepDelta(scope=doc_scope(slug), step="extract", seq=seq, added=g,
                  parent_seq=seq - 1),
        delta_path(tmp_path, doc_scope(slug), seq),
    )


# ── threshold filtering ──────────────────────────────────────────────────


def test_promote_skips_class_below_threshold(tmp_path):
    """Class declared in only 1 doc doesn't meet threshold=2 — no promotion."""
    _seed_doc_with_ext_class(tmp_path, "doc-a",
                              ExtClass(slug="Invoice",
                                       anchor=LIS.InformationObject,
                                       label="Invoice"))
    decisions = walk_promote(tmp_path, threshold=2)
    assert decisions == []
    # No project-scope delta written
    assert list_deltas_for_scope(tmp_path, project_scope()) == []


def test_promote_emits_project_delta_when_threshold_met(tmp_path):
    """Same class declared in 2 docs → promoted to project scope."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                   label="Invoice", comment="A bill.")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    decisions = walk_promote(tmp_path, threshold=2)
    assert len(decisions) == 1
    assert decisions[0].slug == "Invoice"
    assert sorted(decisions[0].contributors) == ["doc-a", "doc-b"]

    # Project-scope delta exists with the canonical class definition
    project_state = materialize(tmp_path, project_scope())
    assert (EXT.Invoice, RDF.type, OWL.Class) in project_state
    assert (EXT.Invoice, RDFS.subClassOf, LIS.InformationObject) in project_state
    assert (EXT.Invoice, RDFS.label, Literal("Invoice")) in project_state


# ── per-doc removals ─────────────────────────────────────────────────────


def test_promote_removes_class_from_contributing_doc_scopes(tmp_path):
    """After promotion, each contributing doc's materialized scope no
    longer has the class declaration — it's now in project scope only."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    walk_promote(tmp_path, threshold=2)

    # Each doc's materialized state no longer declares the class
    for slug in ("doc-a", "doc-b"):
        state = materialize(tmp_path, doc_scope(slug))
        assert (EXT.Invoice, RDF.type, OWL.Class) not in state
        assert (EXT.Invoice, RDFS.subClassOf, LIS.InformationObject) not in state


def test_promote_preserves_instance_triples_in_contributors(tmp_path):
    """The promote step removes only class metadata; instance triples
    `<entity> rdf:type ext:Foo` stay in the contributing docs."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    EX = URIRef("http://example.org/test/")

    # Doc A has the class + an instance
    g_a = Graph()
    for t in class_definitions_graph([cls]):
        g_a.add(t)
    g_a.add((URIRef("http://example.org/inv-a"), RDF.type, EXT.Invoice))
    write_delta(
        StepDelta(scope=doc_scope("doc-a"), step="extract", seq=1, added=g_a),
        delta_path(tmp_path, doc_scope("doc-a"), 1),
    )
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    walk_promote(tmp_path, threshold=2)

    state = materialize(tmp_path, doc_scope("doc-a"))
    # Class metadata gone
    assert (EXT.Invoice, RDF.type, OWL.Class) not in state
    # Instance triple still there (now pointing at the project-scope class)
    assert (URIRef("http://example.org/inv-a"), RDF.type, EXT.Invoice) in state


# ── merging across multiple docs ─────────────────────────────────────────


def test_promote_unions_alt_labels_across_contributors(tmp_path):
    """When the same class is declared with DIFFERENT altLabels in
    different docs, the promoted canonical accumulates the union."""
    cls_a = ExtClass(slug="IBAN", anchor=LIS.InformationObject,
                     label="IBAN", alt_labels=["BankAccountNumber"])
    cls_b = ExtClass(slug="IBAN", anchor=LIS.InformationObject,
                     label="IBAN", alt_labels=["InternationalBankAccountNumber"])
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls_a)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls_b)

    walk_promote(tmp_path, threshold=2)
    project_state = materialize(tmp_path, project_scope())
    alts = {str(o) for o in project_state.objects(EXT.IBAN, SKOS.altLabel)}
    assert "BankAccountNumber"              in alts
    assert "InternationalBankAccountNumber" in alts


def test_promote_audits_contributors_via_firstSeenIn(tmp_path):
    """Each promotion records `dg:firstSeenIn <doc-scope-uri>` per
    contributor — audit trail of which docs proposed the class."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    walk_promote(tmp_path, threshold=2)
    project_state = materialize(tmp_path, project_scope())
    contribs = {str(o) for o in project_state.objects(EXT.Invoice, DG.firstSeenIn)}
    assert "urn:docgraph:scope/doc/doc-a" in contribs
    assert "urn:docgraph:scope/doc/doc-b" in contribs


# ── idempotency: re-running doesn't double-promote ───────────────────────


def test_promote_skips_already_promoted_classes(tmp_path):
    """Once a class is in project scope, subsequent walk_promote runs
    don't re-promote it (would create redundant project delta + double
    removal from doc scopes)."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    decisions1 = walk_promote(tmp_path, threshold=2)
    decisions2 = walk_promote(tmp_path, threshold=2)
    assert len(decisions1) == 1
    assert decisions2 == []          # second run no-ops


# ── higher thresholds ───────────────────────────────────────────────────


def test_promote_with_higher_threshold(tmp_path):
    """threshold=3 requires 3 contributing docs; 2 isn't enough."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    decisions = walk_promote(tmp_path, threshold=3)
    assert decisions == []

    _seed_doc_with_ext_class(tmp_path, "doc-c", cls)
    decisions = walk_promote(tmp_path, threshold=3)
    assert len(decisions) == 1


# ── no doc deltas → no promotions ───────────────────────────────────────


def test_promote_with_no_doc_scopes(tmp_path):
    """Empty project — no deltas — no promotions."""
    decisions = walk_promote(tmp_path, threshold=2)
    assert decisions == []
