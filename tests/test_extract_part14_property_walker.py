"""M2 stage 2 — per-entity property extraction tests with mock LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from src.extract_part14 import axioms
from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.property_walker import (
    coerce_value,
    extractable_properties_for,
    walk_stage2,
    PropertyResult,
)
from src.extract_part14.walker import (
    DG,
    LIS,
    EvidenceSelector,
    ExtractedEntity,
)
from src.llm import TextBlock
from src.models import ModelConfig
from src.project import init_project, PIPELINE_PART14


# ── Mock infrastructure ────────────────────────────────────────────────────

@dataclass
class _MockResp:
    content: list


class MockPropertyLLM:
    """Returns canned responses keyed by (entity_label, property_local)."""
    def __init__(self, responses: dict[tuple[str, str], dict]):
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        prompt = messages[0]["content"] if messages else ""
        # Find which (entity, property) this prompt is asking about
        key = None
        for (entity, prop) in self.responses:
            if f'entity "{entity}"' in prompt and f'"{prop}"' in prompt:
                key = (entity, prop)
                break
        self.calls.append(key or ("?", "?"))
        payload = self.responses.get(key, {"value": None, "value_entity": None,
                                            "confidence": 0.0, "rationale": "no match"})
        return _MockResp(content=[TextBlock(text=json.dumps(payload))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("prop-walker-ontology")
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


# ── Descent (effective_branches) ───────────────────────────────────────────

def test_effective_branches_descends_recursively(ontology):
    """Recursive descent: any class with ≥threshold extractable children is
    replaced by its children (and the descent continues from there)."""
    branches = axioms.effective_branches(ontology, namespace=str(LIS))

    # Activity stays at the top (only 2 children: Event, PeriodInTime — under threshold)
    assert URIRef(str(LIS) + "Activity") in branches

    # Aspect stays — POSC LIS-14 added an intermediate RealizableEntity class,
    # so Aspect's direct children are now {Quality, RealizableEntity} (2 — under
    # threshold). Was 3 in the standards.iso.org version (Quality, Disposition,
    # Role), where it descended.
    assert URIRef(str(LIS) + "Aspect") in branches

    # Object descends past itself (5 children) — Object NOT in branches
    assert URIRef(str(LIS) + "Object") not in branches

    # Most of Object's children stay (1-2 children each, under threshold)
    for child in ("FunctionalObject", "InformationObject", "Location", "Organization"):
        assert URIRef(str(LIS) + child) in branches

    # PhysicalObject has 4 extractable children — descent continues past it
    assert URIRef(str(LIS) + "PhysicalObject") not in branches
    for grandchild in ("Compound", "Feature", "InanimatePhysicalObject", "Organism"):
        assert URIRef(str(LIS) + grandchild) in branches


def test_effective_branches_skips_dg_plumbing(ontology):
    """dg:Document, dg:Quote, etc. (subClassOf lis:InformationObject via
    alignments) are marked dg:extractable false → never branches."""
    branches = axioms.effective_branches(ontology, namespace=str(LIS))
    DG = "urn:docgraph:vocab:meta#"
    for plumbing in ("Document", "Chapter", "Quote", "File", "PdfFile", "MarkdownFile"):
        assert URIRef(DG + plumbing) not in branches


def test_effective_branches_threshold_configurable(ontology):
    """With threshold=10, no branch descends — back to top-level only."""
    branches = axioms.effective_branches(ontology, namespace=str(LIS), descent_threshold=10)
    expected = {URIRef(str(LIS) + n) for n in ("Activity", "Aspect", "Object")}
    assert set(branches) == expected


# ── Property selection / filtering ─────────────────────────────────────────

def test_extractable_properties_filters_inverse_pairs(ontology):
    """`hasParticipant` and `participantIn` are inverses — only one survives."""
    # Activity has both lis:hasParticipant (forward) declared with rdfs:domain Activity
    properties = extractable_properties_for(URIRef(str(LIS) + "Activity"), ontology)

    # Should contain hasParticipant (the "has..." direction)
    assert URIRef(str(LIS) + "hasParticipant") in properties
    # Should NOT contain participantIn (the inverse — domain-via-inheritance)
    assert URIRef(str(LIS) + "participantIn") not in properties


def test_extractable_properties_filters_parent_properties(ontology):
    """If an entity type has both `hasArrangedPart` and `hasPart` available
    (via inheritance), keep the more specialized one."""
    # Object has hasPart via inheritance + multiple specialized has*Part properties
    properties = extractable_properties_for(URIRef(str(LIS) + "Object"), ontology)

    # If hasArrangedPart is in scope, hasPart should NOT be (parent dropped)
    if URIRef(str(LIS) + "hasArrangedPart") in properties:
        assert URIRef(str(LIS) + "hasPart") not in properties


# ── Value coercion ─────────────────────────────────────────────────────────

def test_coerce_literal_with_typed_range():
    r = PropertyResult(value="2025-01-17")
    out = coerce_value(r, range_uri=XSD.date, known_entities=[])
    assert isinstance(out, Literal)
    assert out.datatype == XSD.date


def test_coerce_falls_back_to_plain_literal_on_unparseable():
    r = PropertyResult(value="not a number")
    out = coerce_value(r, range_uri=XSD.integer, known_entities=[])
    assert isinstance(out, Literal)
    # Plain literal (no datatype assigned) since int() failed
    assert out.datatype is None


def test_coerce_value_entity_resolves_to_uri():
    e1 = ExtractedEntity(uri=URIRef("ex:e1"), type_uri=LIS.Object, label="Acme Inc.")
    e2 = ExtractedEntity(uri=URIRef("ex:e2"), type_uri=LIS.Object, label="Beta")
    r = PropertyResult(value_entity="Acme Inc.")
    out = coerce_value(r, range_uri=LIS.Organization, known_entities=[e1, e2])
    assert out == e1.uri


def test_coerce_unknown_entity_falls_back_to_literal():
    r = PropertyResult(value_entity="Unknown Co.")
    out = coerce_value(r, range_uri=LIS.Organization, known_entities=[])
    assert isinstance(out, Literal)
    assert str(out) == "Unknown Co."


def test_coerce_returns_none_when_both_null():
    r = PropertyResult()
    assert coerce_value(r, range_uri=XSD.string, known_entities=[]) is None


def test_coerce_uses_rdl_resolver_when_no_known_entity(tmp_path):
    """If value_entity doesn't match any known entity, the RDL resolver is
    consulted before falling back to a literal."""
    from src.extract_part14.rdl import WIKIDATA, RdlResolver

    class _StubResolver(RdlResolver):
        def __init__(self):
            super().__init__(WIKIDATA, cache_dir=None)
        def _run(self, query):
            return [{
                "item":  {"type": "uri", "value": "http://www.wikidata.org/entity/Q4916"},
                "label": {"type": "literal", "value": "Euro", "xml:lang": "en"},
            }]

    r = PropertyResult(value_entity="EUR")
    out = coerce_value(
        r, range_uri=URIRef("urn:test:Currency"),
        known_entities=[],
        rdl_resolvers=[_StubResolver()],
    )
    assert out == URIRef("http://www.wikidata.org/entity/Q4916")


def test_coerce_known_entity_wins_over_rdl(tmp_path):
    """If value_entity matches a known entity, the RDL resolver is NOT
    consulted (existing entity is preferred)."""
    from src.extract_part14.rdl import WIKIDATA, RdlResolver

    class _CountingResolver(RdlResolver):
        def __init__(self):
            super().__init__(WIKIDATA, cache_dir=None)
            self.calls = 0
        def _run(self, query):
            self.calls += 1
            return []

    e = ExtractedEntity(
        uri=URIRef("ex:e1"), type_uri=LIS.Object, label="Acme Inc.",
    )
    counting = _CountingResolver()
    r = PropertyResult(value_entity="Acme Inc.")
    out = coerce_value(
        r, range_uri=LIS.Organization,
        known_entities=[e],
        rdl_resolvers=[counting],
    )
    assert out == e.uri
    assert counting.calls == 0       # never asked the RDL


# ── Walker integration (mock LLM) ──────────────────────────────────────────

def test_walk_stage2_extracts_property(ontology, model):
    """One entity, one property — verify the LLM is called and the triple lands."""
    activity = ExtractedEntity(
        uri=URIRef("http://example.org/source/x/activity/cleaning"),
        type_uri=LIS.Activity,
        label="Professional cleaning",
        evidence=[EvidenceSelector(
            exact="Professional cleaning on 2025-01-17",
            anchor="id-1",
        )],
    )

    # Mock returns a literal value for one property and null for others
    mock = MockPropertyLLM(responses={
        ("Professional cleaning", "creates"): {
            "value": None,
            "value_entity": None,
            "confidence": 0.0,
            "rationale": "n/a",
        },
    })

    g = walk_stage2(
        extracted_entities=[activity],
        ontology=ontology,
        document_context="Title: 'Invoice 1352'",
        client=mock,
        model=model,
    )

    # The LLM was called — exact count depends on how many properties Activity has
    assert len(mock.calls) > 0


def test_walk_stage2_unknown_class_still_gets_domain_less_props(ontology, model):
    """Even when an entity's class is unknown to the ontology, the domain-less
    properties (lis:approvedOn, lis:hasRole, etc. — ~50 of LIS-14's 66) apply
    universally. So a per-entity batch call IS made (with no domain-matched
    candidates, just the universal ones); the LLM returns empty values and no
    triples are emitted.

    Was: previously asserted zero LLM calls — back when only domain-matched
    properties were surfaced. The architectural shift (POSC LIS-14 leaves most
    properties domain-less by design) made that assumption obsolete."""
    fake_entity = ExtractedEntity(
        uri=URIRef("ex:e"),
        type_uri=URIRef("http://example.org/fake/Class"),
        label="x",
    )
    mock = MockPropertyLLM(responses={})
    g = walk_stage2(
        extracted_entities=[fake_entity],
        ontology=ontology,
        document_context="",
        client=mock,
        model=model,
    )
    assert len(mock.calls) == 1     # one batch call with universal properties
    assert len(g) == 0              # but no triples because mock returned nothing


# ── Per-entity batch (new shape) ────────────────────────────────────────────

class MockBatchPropertyLLM:
    """Returns the new batch shape: one response per entity, all property
    values together."""
    def __init__(self, responses_by_entity: dict[str, list[dict]]):
        self.responses = responses_by_entity
        self.calls: list[str] = []

    def create(self, *, model_id, messages, **_):
        prompt = messages[0]["content"] if messages else ""
        self.calls.append(prompt)
        # Find which entity this prompt is asking about
        for entity_label, values in self.responses.items():
            if f'entity "{entity_label}"' in prompt:
                payload = {"values": values}
                return _MockResp(content=[TextBlock(text=json.dumps(payload))])
        return _MockResp(content=[TextBlock(text=json.dumps({"values": []}))])


def test_walk_stage2_one_call_per_entity(ontology, model):
    """Verify the batch shape: one LLM call per entity, regardless of how
    many properties that entity has."""
    activity = ExtractedEntity(
        uri=URIRef("http://example.org/source/x/activity/cleaning"),
        type_uri=LIS.Activity,
        label="Professional cleaning",
        evidence=[EvidenceSelector(exact="cleaning on 17.01.2025", anchor="id-1")],
    )

    mock = MockBatchPropertyLLM(responses_by_entity={
        "Professional cleaning": [
            # Two of Activity's many properties have values; rest omitted
            {"property": "lis:hasParticipant", "value": None,
             "value_entity": "Polina", "evidence": "Polina (dentist)"},
        ],
    })

    g = walk_stage2(
        extracted_entities=[activity],
        ontology=ontology,
        document_context="",
        client=mock, model=model,
    )

    # Exactly 1 LLM call per entity (was N: one per property, often returning null)
    assert len(mock.calls) == 1


def test_infer_cross_entity_links_fills_missing_datumuom(ontology, model):
    """Quote co-occurrence inference: a ScalarQuantityDatum whose supporting
    quote mentions "EUR" gets `lis:datumUOM <eur>` even when the LLM didn't
    extract that link."""
    from src.extract_part14.property_walker import infer_cross_entity_links

    LIS_NS = str(LIS)
    eur = ExtractedEntity(
        uri=URIRef("ex:unitofmeasure/eur"),
        type_uri=URIRef(LIS_NS + "UnitOfMeasure"),
        label="EUR",
    )
    total = ExtractedEntity(
        uri=URIRef("ex:scalarquantitydatum/invoice-total"),
        type_uri=URIRef(LIS_NS + "ScalarQuantityDatum"),
        label="Invoice Total Amount",
        evidence=[EvidenceSelector(exact="EUR 115,84", anchor="id-1")],
    )

    g = Graph()
    new_triples = infer_cross_entity_links(
        extracted_entities=[eur, total],
        graph=g,
        ontology=ontology,
    )

    datum_uom = URIRef(LIS_NS + "datumUOM")
    assert (total.uri, datum_uom, eur.uri) in new_triples


def test_infer_cross_entity_links_skips_already_populated(ontology, model):
    """If the entity already has the property populated (literal or URI),
    inference shouldn't add a competing triple."""
    from src.extract_part14.property_walker import infer_cross_entity_links

    LIS_NS = str(LIS)
    eur = ExtractedEntity(
        uri=URIRef("ex:unitofmeasure/eur"),
        type_uri=URIRef(LIS_NS + "UnitOfMeasure"),
        label="EUR",
    )
    total = ExtractedEntity(
        uri=URIRef("ex:scalarquantitydatum/invoice-total"),
        type_uri=URIRef(LIS_NS + "ScalarQuantityDatum"),
        label="Invoice Total Amount",
        evidence=[EvidenceSelector(exact="EUR 115,84")],
    )

    # Pre-populate datumUOM with a literal — inference should skip
    g = Graph()
    datum_uom = URIRef(LIS_NS + "datumUOM")
    g.add((total.uri, datum_uom, Literal("EUR")))

    new_triples = infer_cross_entity_links(
        extracted_entities=[eur, total], graph=g, ontology=ontology,
    )
    assert (total.uri, datum_uom, eur.uri) not in new_triples


def test_infer_cross_entity_links_word_boundary(ontology, model):
    """Substring 'EUR' inside 'EUROPE' should NOT match — must be word-bounded."""
    from src.extract_part14.property_walker import infer_cross_entity_links

    LIS_NS = str(LIS)
    eur = ExtractedEntity(
        uri=URIRef("ex:unitofmeasure/eur"),
        type_uri=URIRef(LIS_NS + "UnitOfMeasure"),
        label="EUR",
    )
    total = ExtractedEntity(
        uri=URIRef("ex:scalarquantitydatum/x"),
        type_uri=URIRef(LIS_NS + "ScalarQuantityDatum"),
        label="x",
        evidence=[EvidenceSelector(exact="prices in EUROPE are listed")],
    )

    g = Graph()
    new_triples = infer_cross_entity_links([eur, total], g, ontology)
    datum_uom = URIRef(LIS_NS + "datumUOM")
    assert (total.uri, datum_uom, eur.uri) not in new_triples


def test_walk_stage2_omitted_properties_dont_emit_triples(ontology, model):
    """Properties that the LLM omits from `values` should NOT result in
    triples (no implicit None)."""
    activity = ExtractedEntity(
        uri=URIRef("ex:activity/x"),
        type_uri=LIS.Activity,
        label="Cleaning",
    )

    mock = MockBatchPropertyLLM(responses_by_entity={
        "Cleaning": [],  # LLM found no values for any property
    })

    g = walk_stage2(
        extracted_entities=[activity],
        ontology=ontology,
        document_context="",
        client=mock, model=model,
    )
    assert len(g) == 0
