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


# ── retire-upward (project-ext → upstream RDL slug match) ───────────────


def _init_project(tmp_path: Path) -> Path:
    """Initialise a minimal Part 14 project so the loader can build a
    dataset that includes LIS-14 + dg as upstream graphs."""
    from rich.console import Console
    from src.project import init_project
    init_project(tmp_path, Console(quiet=True))
    return tmp_path


def test_consolidate_retires_project_class_when_upstream_has_same_slug(tmp_path):
    """When a project-ext class shares its slug with an upstream RDL class
    (LIS-14, dg, etc.), the retire-upward pass marks the project-ext
    class deprecated and rewrites contributing-doc instance triples to
    the upstream URI."""
    _init_project(tmp_path)
    # `lis:InformationObject` exists in LIS-14. Seed two docs each
    # proposing `InformationObject` so mint-upward elevates it to
    # `ext:InformationObject`; retire-upward should then deprecate it
    # in favor of the LIS-14 canonical.
    cls = ExtClass(slug="InformationObject", anchor=LIS.InformationObject,
                   label="InformationObject")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)
    # Add an instance in each doc typed at the doc-local URI so we can
    # observe end-to-end retyping doc-local → ext: → upstream.
    for slug in ("doc-a", "doc-b"):
        g = Graph()
        local_uri = _doc_local_ns(slug).InformationObject
        inst = URIRef(f"http://example.org/{slug}/inst")
        g.add((inst, RDF.type, local_uri))
        write_delta(
            StepDelta(scope=doc_scope(slug), step="extract", seq=2, added=g,
                      parent_seq=1),
            delta_path(tmp_path, doc_scope(slug), 2),
        )

    walk_consolidate(tmp_path, threshold=2)

    project_state = materialize(tmp_path, project_scope())
    # The retired project-ext class carries the deprecation triple set,
    # all three pointing at the upstream LIS-14 URI.
    upstream = LIS.InformationObject
    assert (EXT.InformationObject, OWL.deprecated,
            Literal(True, datatype=XSD.boolean))                in project_state
    assert (EXT.InformationObject, OWL.equivalentClass, upstream) in project_state
    assert (EXT.InformationObject, DCTERMS.isReplacedBy, upstream) in project_state

    # And each doc's instance is now typed directly at the upstream URI.
    for slug in ("doc-a", "doc-b"):
        state = materialize(tmp_path, doc_scope(slug))
        inst  = URIRef(f"http://example.org/{slug}/inst")
        assert (inst, RDF.type, upstream)               in state
        assert (inst, RDF.type, EXT.InformationObject)  not in state


def test_consolidate_no_retire_when_no_upstream_match(tmp_path):
    """A project-ext class whose slug has no upstream equivalent
    survives the retire pass unchanged."""
    _init_project(tmp_path)
    cls = ExtClass(slug="ZahnPraxis",       # made-up slug, not in any RDL
                   anchor=LIS.InformationObject,
                   label="ZahnPraxis")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    walk_consolidate(tmp_path, threshold=2)
    project_state = materialize(tmp_path, project_scope())
    # Class was minted to project scope, NOT retired.
    assert (EXT.ZahnPraxis, RDF.type, OWL.Class)                   in project_state
    assert (EXT.ZahnPraxis, OWL.deprecated,
            Literal(True, datatype=XSD.boolean))                   not in project_state
    assert (EXT.ZahnPraxis, DCTERMS.isReplacedBy, None) not in [
        (s, p, None) for s, p, o in project_state.triples(
            (EXT.ZahnPraxis, DCTERMS.isReplacedBy, None))
    ]


def test_consolidate_follows_deprecation_chain_for_new_doc(tmp_path):
    """After a project-ext class has been retired upward to an upstream
    canonical (e.g., ext:InformationObject → lis:InformationObject), a
    NEWLY added doc that proposes the same slug should have its
    doc-local URI deprecated DIRECTLY to the upstream canonical —
    skipping the deprecated intermediate. Otherwise the new doc's
    instances would orphan at the doc-local URI forever."""
    _init_project(tmp_path)
    cls = ExtClass(slug="InformationObject", anchor=LIS.InformationObject,
                   label="InformationObject")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)
    # First consolidate: mint ext:InformationObject, then retire to lis:InformationObject.
    walk_consolidate(tmp_path, threshold=2)

    # Now add a third doc with the same slug + an instance typed locally.
    _seed_doc_with_ext_class(tmp_path, "doc-c", cls, seq=1)
    g = Graph()
    local_uri_c = _doc_local_ns("doc-c").InformationObject
    inst = URIRef("http://example.org/doc-c/inst")
    g.add((inst, RDF.type, local_uri_c))
    write_delta(
        StepDelta(scope=doc_scope("doc-c"), step="extract", seq=2, added=g,
                  parent_seq=1),
        delta_path(tmp_path, doc_scope("doc-c"), 2),
    )

    walk_consolidate(tmp_path, threshold=2)

    # Doc-C's doc-local URI is now deprecated AND points at the upstream
    # canonical directly (not at the intermediate ext:InformationObject).
    state_c = materialize(tmp_path, doc_scope("doc-c"))
    upstream = LIS.InformationObject
    assert (local_uri_c, OWL.deprecated,
            Literal(True, datatype=XSD.boolean))                in state_c
    assert (local_uri_c, OWL.equivalentClass, upstream)         in state_c
    assert (local_uri_c, DCTERMS.isReplacedBy, upstream)        in state_c
    # NOT pointing at the deprecated intermediate.
    assert (local_uri_c, DCTERMS.isReplacedBy, EXT.InformationObject) not in state_c

    # Doc-C's instance is typed directly at the upstream URI.
    assert (inst, RDF.type, upstream)    in state_c
    assert (inst, RDF.type, local_uri_c) not in state_c


def test_consolidate_retire_is_idempotent(tmp_path):
    """Re-running consolidate doesn't re-emit retire triples for a class
    already marked deprecated."""
    _init_project(tmp_path)
    cls = ExtClass(slug="InformationObject", anchor=LIS.InformationObject,
                   label="InformationObject")
    _seed_doc_with_ext_class(tmp_path, "doc-a", cls)
    _seed_doc_with_ext_class(tmp_path, "doc-b", cls)

    walk_consolidate(tmp_path, threshold=2)
    deltas_after_first = list_deltas_for_scope(tmp_path, project_scope())

    walk_consolidate(tmp_path, threshold=2)
    deltas_after_second = list_deltas_for_scope(tmp_path, project_scope())

    # Second run produces no new project-scope deltas.
    assert deltas_after_second == deltas_after_first
