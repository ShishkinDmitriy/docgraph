"""Tests for `align_doc` — single-doc alignment to higher-scope classes.

Runs at the end of the `add` pipeline so each doc is self-consistent on
first ingest (no doc-local URI duplicating a project-ext or upstream
canonical). See docs/architecture/rdl-scopes.md.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rich.console import Console

from src.deltas import (
    StepDelta,
    delta_path,
    doc_scope,
    list_deltas_for_scope,
    materialize,
    write_delta,
)
from src.extract_part14.align import align_doc
from src.extract_part14.consolidate import EXT
from src.extract_part14.ext_ontology import (
    DG,
    LIS,
    ExtClass,
    class_definitions_graph,
)
from src.tasks.init import init_project

DCTERMS = Namespace("http://purl.org/dc/terms/")
DEPRECATED = Literal(True, datatype=XSD.boolean)


def _doc_local_ns(slug: str) -> Namespace:
    return Namespace(f"urn:docgraph:source:{slug}/")


def _seed_doc(tmp_path: Path, slug: str, classes: list[ExtClass],
              instances: list[tuple[URIRef, URIRef]] = ()) -> None:
    """Write a doc-scope extract delta with classes (re-keyed into the
    doc's local namespace) + optional `(entity, class_uri)` instances."""
    local_classes = [
        ExtClass(slug=c.slug, anchor=c.anchor, label=c.label,
                 alt_labels=list(c.alt_labels), comment=c.comment,
                 provenance=c.provenance, first_seen=c.first_seen,
                 namespace=_doc_local_ns(slug))
        for c in classes
    ]
    g = Graph()
    for t in class_definitions_graph(local_classes):
        g.add(t)
    for ent, cls_uri in instances:
        g.add((ent, RDF.type, cls_uri))
    write_delta(
        StepDelta(scope=doc_scope(slug), step="extract", seq=1, added=g,
                  parent_seq=0),
        delta_path(tmp_path, doc_scope(slug), 1),
    )


def _project(tmp_path: Path) -> Path:
    init_project(tmp_path, Console(quiet=True))
    return tmp_path


# ── slug match against upstream RDL (LIS-14) ────────────────────────────


def test_align_routes_doc_local_to_upstream_when_slug_matches(tmp_path):
    """A doc proposes `<doc/InformationObject>` (matching LIS-14's
    `lis:InformationObject`). Alignment deprecates the doc-local URI
    onto the upstream canonical and retypes instances."""
    _project(tmp_path)
    cls = ExtClass(slug="InformationObject", anchor=LIS.InformationObject,
                   label="InformationObject")
    inst = URIRef("http://example.org/doc-a/inst")
    local_uri = _doc_local_ns("doc-a").InformationObject
    _seed_doc(tmp_path, "doc-a", [cls], instances=[(inst, local_uri)])

    aligned = align_doc(tmp_path, "doc-a")
    assert aligned == 1

    state = materialize(tmp_path, doc_scope("doc-a"))
    # Doc-local class definition stays (lifecycle invariant) + carries
    # deprecation triples pointing at the upstream canonical.
    assert (local_uri, RDF.type, OWL.Class)                in state
    assert (local_uri, OWL.deprecated, DEPRECATED)         in state
    assert (local_uri, OWL.equivalentClass,  LIS.InformationObject) in state
    assert (local_uri, DCTERMS.isReplacedBy, LIS.InformationObject) in state
    # Instance retyped to the upstream URI.
    assert (inst, RDF.type, local_uri)                     not in state
    assert (inst, RDF.type, LIS.InformationObject)         in state


# ── slug match against project-ext canonical ────────────────────────────


def test_align_routes_doc_local_to_project_ext_when_slug_matches(tmp_path):
    """A doc proposes `<doc/Invoice>` while `ext:Invoice` already exists
    at project scope (e.g., promoted by a prior consolidate run).
    Alignment routes the new doc's instances onto `ext:Invoice`."""
    _project(tmp_path)
    # Seed project-scope with a promoted ext:Invoice (simulating prior
    # consolidate output).
    project_cls = ExtClass(
        slug="Invoice", anchor=LIS.InformationObject,
        label="Invoice", provenance="promoted",
    )
    pg = Graph()
    for t in class_definitions_graph([project_cls]):
        pg.add(t)
    from src.deltas import project_scope
    write_delta(
        StepDelta(scope=project_scope(), step="consolidate", seq=1,
                  added=pg, parent_seq=0),
        delta_path(tmp_path, project_scope(), 1),
    )

    # New doc with its own Invoice proposal.
    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                   label="Invoice")
    inst = URIRef("http://example.org/doc-b/inv")
    local_uri = _doc_local_ns("doc-b").Invoice
    _seed_doc(tmp_path, "doc-b", [cls], instances=[(inst, local_uri)])

    aligned = align_doc(tmp_path, "doc-b")
    assert aligned == 1

    state = materialize(tmp_path, doc_scope("doc-b"))
    assert (local_uri, OWL.deprecated, DEPRECATED)        in state
    assert (local_uri, OWL.equivalentClass, EXT.Invoice)  in state
    assert (inst, RDF.type, EXT.Invoice)                  in state


# ── follows chain when project-ext is itself deprecated ─────────────────


def test_align_follows_one_deprecation_hop(tmp_path):
    """ext:Invoice exists at project scope but is itself deprecated
    onto an upstream URI. A doc-local Invoice should align DIRECTLY to
    the upstream URI, not the deprecated intermediate."""
    _project(tmp_path)
    # Project scope: ext:Invoice deprecated onto a hypothetical upstream.
    upstream = URIRef("http://example.org/upstream/Invoice")
    project_cls = ExtClass(
        slug="Invoice", anchor=LIS.InformationObject,
        label="Invoice", provenance="promoted",
    )
    pg = Graph()
    for t in class_definitions_graph([project_cls]):
        pg.add(t)
    pg.add((EXT.Invoice, OWL.deprecated,        DEPRECATED))
    pg.add((EXT.Invoice, OWL.equivalentClass,   upstream))
    pg.add((EXT.Invoice, DCTERMS.isReplacedBy,  upstream))
    # The upstream URI must exist as an owl:Class for the alignment scan
    # to find it (the loader's union view would normally hold this).
    pg.add((upstream, RDF.type, OWL.Class))
    from src.deltas import project_scope
    write_delta(
        StepDelta(scope=project_scope(), step="consolidate", seq=1,
                  added=pg, parent_seq=0),
        delta_path(tmp_path, project_scope(), 1),
    )

    cls = ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice")
    inst = URIRef("http://example.org/doc-c/inv")
    local_uri = _doc_local_ns("doc-c").Invoice
    _seed_doc(tmp_path, "doc-c", [cls], instances=[(inst, local_uri)])

    align_doc(tmp_path, "doc-c")
    state = materialize(tmp_path, doc_scope("doc-c"))
    # Doc-local Invoice replaced by upstream (skipped deprecated middle).
    assert (local_uri, DCTERMS.isReplacedBy, upstream)    in state
    assert (local_uri, DCTERMS.isReplacedBy, EXT.Invoice) not in state
    assert (inst, RDF.type, upstream)                      in state


# ── idempotency ─────────────────────────────────────────────────────────


def test_align_is_idempotent(tmp_path):
    """Re-running align on the same doc produces no further deltas."""
    _project(tmp_path)
    cls = ExtClass(slug="InformationObject", anchor=LIS.InformationObject,
                   label="InformationObject")
    _seed_doc(tmp_path, "doc-a", [cls])

    align_doc(tmp_path, "doc-a")
    deltas_after_first = list_deltas_for_scope(tmp_path, doc_scope("doc-a"))

    align_doc(tmp_path, "doc-a")
    deltas_after_second = list_deltas_for_scope(tmp_path, doc_scope("doc-a"))

    assert deltas_after_second == deltas_after_first


# ── no false-positive alignment ─────────────────────────────────────────


def test_align_leaves_genuinely_new_doc_local_classes_alone(tmp_path):
    """A doc-local class whose slug has NO higher-scope equivalent
    survives unchanged. Alignment doesn't invent matches."""
    _project(tmp_path)
    cls = ExtClass(slug="ZahnPraxis", anchor=LIS.InformationObject,
                   label="ZahnPraxis")
    _seed_doc(tmp_path, "doc-a", [cls])

    aligned = align_doc(tmp_path, "doc-a")
    assert aligned == 0

    state = materialize(tmp_path, doc_scope("doc-a"))
    local_uri = _doc_local_ns("doc-a").ZahnPraxis
    assert (local_uri, RDF.type, OWL.Class)        in state
    assert (local_uri, OWL.deprecated, DEPRECATED) not in state
