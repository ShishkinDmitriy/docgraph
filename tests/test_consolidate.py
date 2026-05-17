"""Tests for the ext-class consolidate phase.

walk_consolidate() scans per-doc graphs for ext: class declarations,
counts contributing docs, and lifts classes meeting the threshold
into project scope. Each contributing doc gets a delta that removes
the doc-local declaration and rewrites instance triples to the
project canonical URI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

DCTERMS = Namespace("http://purl.org/dc/terms/")

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
from src.extract_part14.consolidate import walk_consolidate


def _doc_local_ns(doc_slug: str) -> Namespace:
    """The per-doc namespace mega_walker mints into."""
    return Namespace(f"urn:docgraph:source:{doc_slug}/")


def _seed_doc_with_ext_class(tmp_path: Path, slug: str, cls: ExtClass,
                              seq: int = 1) -> None:
    """Helper: write a doc-scope seq-N delta that declares one ext class
    at the doc's OWN namespace — matching what `mega_walker` produces in
    real runs. (The test author passes an ExtClass without setting
    `namespace`; we re-key it to the doc's source namespace.)"""
    local_cls = ExtClass(
        slug       = cls.slug,
        anchor     = cls.anchor,
        label      = cls.label,
        alt_labels = list(cls.alt_labels),
        comment    = cls.comment,
        provenance = cls.provenance,
        first_seen = cls.first_seen,
        namespace  = _doc_local_ns(slug),
    )
    g = Graph()
    for t in class_definitions_graph([local_cls]):
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
    decisions = walk_consolidate(tmp_path, threshold=2)
    assert decisions == []
    # No project-scope delta written
    assert list_deltas_for_scope(tmp_path, project_scope()) == []


def test_promote_emits_project_delta_when_threshold_met(tmp_path):
    """Same class declared in 2 docs → promoted to project scope."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                   label="Invoice", comment="A bill.")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    decisions = walk_consolidate(tmp_path, threshold=2)
    assert len(decisions) == 1
    assert decisions[0].slug == "Invoice"
    assert sorted(decisions[0].contributors) == ["doc-a", "doc-b"]

    # Project-scope delta exists with the canonical class definition
    project_state = materialize(tmp_path, project_scope())
    assert (EXT.Invoice, RDF.type, OWL.Class) in project_state
    assert (EXT.Invoice, RDFS.subClassOf, LIS.InformationObject) in project_state
    assert (EXT.Invoice, RDFS.label, Literal("Invoice")) in project_state


# ── per-doc removals ─────────────────────────────────────────────────────


def test_consolidate_deprecates_doc_local_class_definitions(tmp_path):
    """Lifecycle invariant (docs/architecture/rdl-scopes.md): a class
    definition never disappears silently. After consolidate, the doc-local
    class definition STAYS in the doc scope but gains deprecation triples
    pointing at the project canonical (owl:deprecated, owl:equivalentClass,
    dcterms:isReplacedBy)."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    walk_consolidate(tmp_path, threshold=2)

    for slug in ("doc-a", "doc-b"):
        state = materialize(tmp_path, doc_scope(slug))
        local_uri = _doc_local_ns(slug).Invoice
        # Doc-local class definition stays.
        assert (local_uri, RDF.type, OWL.Class)                       in state
        assert (local_uri, RDFS.subClassOf, LIS.InformationObject)    in state
        # Deprecation triple set, all pointing at the project canonical.
        assert (local_uri, OWL.deprecated,
                Literal(True, datatype=XSD.boolean))                   in state
        assert (local_uri, OWL.equivalentClass,   EXT.Invoice)         in state
        assert (local_uri, DCTERMS.isReplacedBy,  EXT.Invoice)         in state


def test_consolidate_rewrites_instance_triples_in_contributors(tmp_path):
    """Instance type triples get rewritten from the doc-local URI to the
    project canonical, so live queries hit the canonical. The doc-local
    class definition itself stays (marked deprecated, with forward
    pointers) — see test_consolidate_deprecates_doc_local_class_definitions."""
    local_ns_a = _doc_local_ns("doc-a")
    cls_local = ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                          label="Invoice", namespace=local_ns_a)

    g_a = Graph()
    for t in class_definitions_graph([cls_local]):
        g_a.add(t)
    inv_a = URIRef("http://example.org/inv-a")
    g_a.add((inv_a, RDF.type, local_ns_a.Invoice))
    write_delta(
        StepDelta(scope=doc_scope("doc-a"), step="extract", seq=1, added=g_a),
        delta_path(tmp_path, doc_scope("doc-a"), 1),
    )
    _seed_doc_with_ext_class(tmp_path, "doc-b",
                              ExtClass(slug="Invoice",
                                       anchor=LIS.InformationObject,
                                       label="Invoice"))

    walk_consolidate(tmp_path, threshold=2)

    state = materialize(tmp_path, doc_scope("doc-a"))
    # Doc-local class definition is still present (just deprecated).
    assert (local_ns_a.Invoice, RDF.type, OWL.Class) in state
    # Instance triple rewritten — now typed against the project ext: URI.
    assert (inv_a, RDF.type, local_ns_a.Invoice) not in state
    assert (inv_a, RDF.type, EXT.Invoice) in state


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

    walk_consolidate(tmp_path, threshold=2)
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

    walk_consolidate(tmp_path, threshold=2)
    project_state = materialize(tmp_path, project_scope())
    contribs = {str(o) for o in project_state.objects(EXT.Invoice, DG.firstSeenIn)}
    assert "urn:docgraph:scope/doc/doc-a" in contribs
    assert "urn:docgraph:scope/doc/doc-b" in contribs


# ── idempotency: re-running doesn't double-promote ───────────────────────


def test_promote_skips_already_promoted_classes(tmp_path):
    """Once a class is in project scope, subsequent walk_consolidate runs
    don't re-promote it (would create redundant project delta + double
    removal from doc scopes)."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    decisions1 = walk_consolidate(tmp_path, threshold=2)
    decisions2 = walk_consolidate(tmp_path, threshold=2)
    assert len(decisions1) == 1
    assert decisions2 == []          # second run no-ops


# ── higher thresholds ───────────────────────────────────────────────────


def test_promote_with_higher_threshold(tmp_path):
    """threshold=3 requires 3 contributing docs; 2 isn't enough."""
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    decisions = walk_consolidate(tmp_path, threshold=3)
    assert decisions == []

    _seed_doc_with_ext_class(tmp_path, "doc-c", cls)
    decisions = walk_consolidate(tmp_path, threshold=3)
    assert len(decisions) == 1


# ── no doc deltas → no promotions ───────────────────────────────────────


def test_promote_with_no_doc_scopes(tmp_path):
    """Empty project — no deltas — no promotions."""
    decisions = walk_consolidate(tmp_path, threshold=2)
    assert decisions == []
