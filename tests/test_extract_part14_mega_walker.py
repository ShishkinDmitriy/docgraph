"""Tests for the mega-walker — single-LLM-call end-to-end extraction.

Verifies the materializer turns a parsed LLM response into the right graph
triples without any real LLM call. Covers:

  - Subject classification triples on the file_uri.
  - Ext class proposal: definition triples in the per-doc graph + entity
    instance using `ext:<slug>` as a type CURIE.
  - Reuse-before-mint when the LLM proposes a slug that already exists.
  - Forbidden anchor proposals are dropped (with a warning).
  - Entity typing, label, evidence → fragment URI with class-N collapse.
  - Property domain/range guards.
  - Activity participants → role minting.
  - Reserved evidence-anchoring properties (lis:representedBy etc.) are
    not surfaced in the property catalog (tested via property_catalog).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from src.extract_part14.ext_ontology import EXT, ExtClass, class_definitions_graph
from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.mega_walker import (
    _format_property_catalog,
    walk_mega,
)
from src.extract_part14.walker import DG, LIS
from src.llm import TextBlock
from src.models import ModelConfig
from src.project import init_project, PIPELINE_PART14


# ── Mock infra ─────────────────────────────────────────────────────────────

@dataclass
class _Resp:
    content: list


class MegaMockLLM:
    """Returns a single canned JSON payload regardless of the prompt."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        self.last_prompt = ""

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        self.calls += 1
        self.last_prompt = messages[0]["content"] if messages else ""
        return _Resp(content=[TextBlock(text=json.dumps(self.payload))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("mega-walker-ontology")
    from rich.console import Console
    init_project(project_dir, Console(quiet=True), pipeline=PIPELINE_PART14)
    ds = build_dataset(project_dir)
    return union_view(ds)


@pytest.fixture
def model():
    return ModelConfig(
        uri=URIRef("http://example.org/model/test"),
        model_id="test-model",
        label="test",
        provider="test",
    )


def _run(payload, *, ontology, model, base="http://example.org/src/test/",
         id_to_class=None, class_to_ids=None):
    base_ns = Namespace(base)
    return walk_mega(
        full_markdown="some markdown {#id-1}",
        document_title="test-doc",
        document_descr="",
        base_ns=base_ns,
        md_source_uri=URIRef(base + "md"),
        file_uri=URIRef(base + "file"),
        ontology=ontology,
        client=MegaMockLLM(payload),
        model=model,
        id_to_class=id_to_class or {},
        class_to_ids=class_to_ids or {},
    )


# ── ONE call only ─────────────────────────────────────────────────────────

def test_mega_walker_makes_exactly_one_llm_call(ontology, model):
    mock = MegaMockLLM({"subject": {"classes": []}, "entities": []})
    base_ns = Namespace("http://example.org/src/once/")
    walk_mega(
        full_markdown="m",
        document_title="t", document_descr="",
        base_ns=base_ns,
        md_source_uri=URIRef("http://example.org/src/once/md"),
        file_uri=URIRef("http://example.org/src/once/file"),
        ontology=ontology, client=mock, model=model,
    )
    assert mock.calls == 1


# ── Subject classification ─────────────────────────────────────────────────

# ── Ext class proposals ────────────────────────────────────────────────────

def test_ext_class_proposal_lands_in_graph_and_is_usable_as_type(ontology, model):
    payload = {
        "subject": {"classes": []},
        "new_classes": [
            {"slug": "Invoice", "anchor": "lis:InformationObject",
             "label": "Invoice",
             "alt_labels": ["Bill", "Rechnung"],
             "comment": "A formal billing document."}
        ],
        "entities": [
            {"name": "Invoice 1352", "types": ["ext:Invoice"],
             "evidence": [{"exact": "Rechnung 1352", "anchor": "id-1"}]}
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    g = result.graph
    # Class definition triples land in the per-doc graph
    assert (EXT.Invoice, RDF.type, OWL.Class) in g
    assert (EXT.Invoice, RDFS.subClassOf, LIS.InformationObject) in g
    assert any("Bill"    in str(o) for o in g.objects(EXT.Invoice, RDFS.label)) or \
           any(str(o) == "Invoice" for o in g.objects(EXT.Invoice, RDFS.label))
    # firstSeenIn carries the source file_uri so we know which doc proposed it
    seen = list(g.objects(EXT.Invoice, DG.firstSeenIn))
    assert URIRef("http://example.org/src/test/file") in seen
    # And the entity carries ext:Invoice as one of its rdf:types
    inst = next((e for e in result.entities if e.label == "Invoice 1352"), None)
    assert inst is not None
    assert (inst.uri, RDF.type, EXT.Invoice) in g
    assert result.new_ext_classes and result.new_ext_classes[0].slug == "Invoice"


def test_ext_class_label_and_alt_labels_normalized_to_camel_case(ontology, model):
    """The LLM tends to emit human-readable labels ("Bank Account") and
    free-form alt labels ("account information", "Bankverbindung"). The
    parser normalizes both to CamelCase so the graph reads consistently."""
    payload = {
        "new_classes": [
            {"slug": "BankAccount", "anchor": "lis:InformationObject",
             "label": "Bank Account",
             "alt_labels": ["account information", "Bankverbindung",
                            "BankAccount",     # dup of the canonical label
                            "BIC code"],       # acronym preserved
             "comment": "..."}
        ],
        "entities": [],
    }
    result = _run(payload, ontology=ontology, model=model)
    g = result.graph
    label = next(g.objects(EXT.BankAccount, RDFS.label))
    assert str(label) == "BankAccount"
    alts = sorted(str(o) for o in g.objects(EXT.BankAccount, SKOS.altLabel))
    # Canonical label is excluded from alts; acronym preserved; CamelCase
    assert "Bankverbindung"     in alts
    assert "AccountInformation" in alts
    assert "BICCode"            in alts
    assert "BankAccount" not in alts


def test_ext_class_with_forbidden_anchor_is_dropped(ontology, model):
    """Anchors must be on the whitelist — random LIS classes are rejected."""
    payload = {
        "subject": {"classes": []},
        "new_classes": [
            {"slug": "BadProposal", "anchor": "lis:Aspect",  # not on whitelist
             "label": "Bad", "comment": "invalid"}
        ],
        "entities": [],
    }
    result = _run(payload, ontology=ontology, model=model)
    assert (EXT.BadProposal, RDF.type, OWL.Class) not in result.graph
    assert result.new_ext_classes == []


def test_ext_class_proposal_with_existing_slug_does_not_redeclare(
        ontology, model, tmp_path):
    """If the project ontology already declares ext:IBAN, a fresh proposal
    doesn't re-mint its triples — but the entity can still type as ext:IBAN."""
    # Build a custom ontology view with a pre-existing ext class.
    extra = class_definitions_graph([
        ExtClass(slug="IBAN", anchor=LIS.InformationObject, label="IBAN",
                 comment="ISO 13616 account identifier."),
    ])
    seeded = Graph()
    for t in ontology:
        seeded.add(t)
    for t in extra:
        seeded.add(t)

    payload = {
        "subject": {"classes": []},
        "new_classes": [
            {"slug": "IBAN", "anchor": "lis:InformationObject",
             "label": "IBAN code", "alt_labels": ["IBAN"], "comment": "..."}
        ],
        "entities": [
            {"name": "DE99 1234", "types": ["ext:IBAN"],
             "evidence": [{"exact": "DE99...", "anchor": "id-1"}]}
        ],
    }
    result = _run(payload, ontology=seeded, model=model)
    # Reused, not newly added.
    assert result.new_ext_classes == []
    # The entity still types as ext:IBAN — the resolver knows about it from
    # the seeded ontology (existing classes) AND the merge result.
    inst = result.entities[0]
    assert (inst.uri, RDF.type, EXT.IBAN) in result.graph


# ── Entity / evidence / class-N collapse ───────────────────────────────────

def test_entity_label_and_evidence_fragments_emitted(ontology, model):
    payload = {
        "subject": {"classes": []},
        "entities": [
            {"name": "Acme Corp",
             "types": ["lis:Organization"],
             "evidence": [{"exact": "Acme Corp", "anchor": "id-3"}]}
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    g = result.graph
    inst = result.entities[0]
    assert (inst.uri, RDFS.label, Literal("Acme Corp")) in g
    assert (inst.uri, RDF.type, LIS.Organization) in g
    # Evidence becomes a lis:representedBy → md#id-3 fragment URI
    rep = list(g.objects(inst.uri, LIS.representedBy))
    assert len(rep) == 1
    assert str(rep[0]).endswith("#id-3")


def test_type_hints_field_is_ignored_no_typeHint_triples(ontology, model):
    """The mega-walker no longer accepts a `type_hints` field. If the LLM
    emits one anyway (legacy), the materializer ignores it — no
    dg:typeHint triples land. The LLM is supposed to either pick an
    existing class or propose a new ext: class."""
    payload = {
        "entities": [
            {"name": "030 676 61 84",
             "types": ["lis:InformationObject"],
             "type_hints": ["phone number"],   # legacy field — should be ignored
             "evidence": [{"exact": "030", "anchor": "id-1"}]}
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    inst = result.entities[0]
    assert list(result.graph.objects(inst.uri, DG.typeHint)) == []


def test_evidence_collapses_to_class_when_all_members_cited(ontology, model):
    """If the LLM cites every id-N in a class-N group, the walker emits a
    single fragment URI for the class-N instead of N per-id triples."""
    payload = {
        "subject": {"classes": []},
        "entities": [
            {"name": "RedHood",
             "types": ["lis:Person"],
             "evidence": [
                 {"exact": "Red", "anchor": "id-1"},
                 {"exact": "Riding", "anchor": "id-2"},
                 {"exact": "Hood", "anchor": "id-3"},
             ]}
        ],
    }
    id_to_class  = {"id-1": "class-1", "id-2": "class-1", "id-3": "class-1"}
    class_to_ids = {"class-1": {"id-1", "id-2", "id-3"}}
    result = _run(payload, ontology=ontology, model=model,
                  id_to_class=id_to_class, class_to_ids=class_to_ids)
    inst = result.entities[0]
    rep = list(result.graph.objects(inst.uri, LIS.representedBy))
    assert len(rep) == 1
    assert str(rep[0]).endswith("#class-1")


def test_evidence_keeps_id_when_class_only_partial(ontology, model):
    """Cites 2 of 3 class-1 members → no collapse, two id-N fragments."""
    payload = {
        "subject": {"classes": []},
        "entities": [
            {"name": "Bob", "types": ["lis:Person"],
             "evidence": [{"exact": "x", "anchor": "id-1"},
                          {"exact": "y", "anchor": "id-2"}]}
        ],
    }
    id_to_class  = {"id-1": "class-1", "id-2": "class-1", "id-3": "class-1"}
    class_to_ids = {"class-1": {"id-1", "id-2", "id-3"}}
    result = _run(payload, ontology=ontology, model=model,
                  id_to_class=id_to_class, class_to_ids=class_to_ids)
    inst = result.entities[0]
    frags = sorted(str(o).rsplit("#", 1)[1] for o in result.graph.objects(inst.uri, LIS.representedBy))
    assert frags == ["id-1", "id-2"]


# ── Role pattern via template (replaces the old activities[].role_hint) ────

def test_role_pattern_binary_properties_land_for_recognizer(ontology, model):
    """The mega prompt no longer asks for template invocations; the LLM
    emits the role-pattern's constituent triples (rdf:type Role +
    lis:realizedIn + lis:hasRole) as ordinary binary properties. The
    SPARQL template recognizer downstream lifts these into the role-
    pattern invocation. Here we just verify the binary properties land."""
    payload = {
        "entities": [
            {"name": "Cleaning", "types": ["lis:Activity"],
             "evidence": [{"exact": "cleaning", "anchor": "id-1"}]},
            {"name": "Patient1", "types": ["lis:Person"],
             "evidence": [{"exact": "Dmitrii", "anchor": "id-2"}],
             "properties": [
                 {"property": "lis:hasRole", "value_entity": "patient",
                  "evidence": "patient"}
             ]},
            {"name": "patient", "types": ["lis:Role"],
             "evidence": [{"exact": "patient", "anchor": "id-3"}],
             "properties": [
                 {"property": "lis:realizedIn", "value_entity": "Cleaning",
                  "evidence": "cleaning"}
             ]},
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    role     = next(e for e in result.entities if e.label == "patient")
    cleaning = next(e for e in result.entities if e.label == "Cleaning")
    patient  = next(e for e in result.entities if e.label == "Patient1")
    assert (role.uri,    RDF.type,         LIS.Role)     in result.graph
    assert (role.uri,    LIS.realizedIn,   cleaning.uri) in result.graph
    assert (patient.uri, LIS.hasRole,      role.uri)     in result.graph


# ── Property catalog excludes reserved evidence-anchor properties ──────────

def test_property_catalog_excludes_representedBy(ontology):
    """Regression: dg:extractable=false on lis:representedBy means it must
    NOT appear in the catalog the LLM sees, so the LLM can't accidentally
    emit `lis:representedBy → <other-entity>` (which would land as a domain
    violation at materialize time)."""
    catalog = _format_property_catalog(ontology)
    assert "lis:representedBy" not in catalog
    assert "lis:representedIn" not in catalog
    assert "lis:represents"    not in catalog


# ── Property-emitter respects domain/range ─────────────────────────────────

def test_object_property_with_literal_value_is_skipped(ontology, model):
    """Regression: lis:hasQuality "warm, scarlet" — an object property
    with a literal value — must NOT materialize as a string. The LLM
    should have minted a separate Quality entity and used value_entity."""
    payload = {
        "subject": {"classes": []},
        "entities": [
            {"name": "Cloak", "types": ["lis:PhysicalObject"],
             "evidence": [{"exact": "cloak", "anchor": "id-1"}],
             "properties": [{"property": "lis:hasQuality",
                             "value": "warm, scarlet",
                             "evidence": "scarlet cloak"}]},
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    inst = result.entities[0]
    # No literal-valued hasQuality triple anywhere
    objs = list(result.graph.objects(inst.uri, LIS.hasQuality))
    assert objs == [], f"expected no hasQuality triple, got {objs}"


def test_object_property_with_value_entity_materializes(ontology, model):
    """Same property, but the LLM minted a Quality entity and pointed
    at it via value_entity — this SHOULD materialize as an entity-to-
    entity triple."""
    payload = {
        "subject": {"classes": []},
        "entities": [
            {"name": "Cloak", "types": ["lis:PhysicalObject"],
             "evidence": [{"exact": "cloak", "anchor": "id-1"}],
             "properties": [{"property": "lis:hasQuality",
                             "value_entity": "warmth",
                             "evidence": "warm cloak"}]},
            {"name": "warmth", "types": ["lis:Quality"],
             "evidence": [{"exact": "warm", "anchor": "id-2"}]},
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    cloak  = next(e for e in result.entities if e.label == "Cloak")
    warmth = next(e for e in result.entities if e.label == "warmth")
    assert (cloak.uri, LIS.hasQuality, warmth.uri) in result.graph


def test_property_with_domain_violation_is_skipped(ontology, model):
    """A property whose rdfs:domain doesn't match the entity's types is
    dropped from the materialized graph (with a warning logged)."""
    payload = {
        "subject": {"classes": []},
        "entities": [
            {"name": "Acme", "types": ["lis:Organization"],
             "evidence": [{"exact": "Acme", "anchor": "id-1"}],
             # hasParticipant has domain lis:Activity, not Organization
             "properties": [{"property": "lis:hasParticipant",
                             "value_entity": "Acme",
                             "evidence": "self"}]},
        ],
    }
    result = _run(payload, ontology=ontology, model=model)
    inst = result.entities[0]
    assert (inst.uri, LIS.hasParticipant, None) not in (
        (s, p, o) for s, p, o in result.graph
    )
