"""Tests for the `enrich` pass — type refinement via RDL + property unlock.

Uses mocked RDL responses + mocked LLM for property extraction. Real network
behavior is exercised by the live Wikidata test in test_extract_part14_rdl.py
(skipped by default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from src.extract_part14.enrich import (
    enrich_source,
    extract_unlocked_properties,
    find_typed_entities,
    refine_types,
)
from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.rdl import WIKIDATA, RdlConfig, RdlResolver, ResolutionResult
from src.extract_part14.walker import DG, LIS, OA, EvidenceSelector, ExtractedEntity
from src.llm import TextBlock
from src.models import ModelConfig
from src.project import (
    PIPELINE_PART14,
    cache_dir,
    graphs_dir,
    init_project,
)


# ── Mock infrastructure ────────────────────────────────────────────────────

@dataclass
class _MockResp:
    content: list


class StubResolver(RdlResolver):
    """Returns canned ResolutionResults keyed by probe substring."""
    def __init__(self, hits: dict[str, ResolutionResult]):
        super().__init__(WIKIDATA, cache_dir=None)
        self.hits = hits
        self.calls: list[str] = []

    def resolve(self, probe: str, *, kind_hint=None) -> ResolutionResult:
        self.calls.append(probe)
        for needle, result in self.hits.items():
            if needle.lower() in probe.lower():
                return result
        return ResolutionResult(uri=None, label="", confidence=0.0)


class StubLLM:
    """LLM mock for the property unlock step. Returns a canned per-entity
    batch response keyed by the entity label substring."""
    def __init__(self, responses_by_entity: dict[str, list[dict]]):
        self.responses = responses_by_entity
        self.calls: list[str] = []

    def create(self, *, model_id, messages, **_):
        prompt = messages[0]["content"] if messages else ""
        self.calls.append(prompt)
        for label, values in self.responses.items():
            if f'entity "{label}"' in prompt:
                return _MockResp(content=[TextBlock(text=json.dumps({"values": values}))])
        return _MockResp(content=[TextBlock(text=json.dumps({"values": []}))])


@pytest.fixture
def part14_project(tmp_path):
    """A real part14 project with one ingested-style graph file containing
    a couple of entities (constructed by hand — no LLM in setup)."""
    from rich.console import Console
    init_project(tmp_path, Console(quiet=True), pipeline=PIPELINE_PART14)

    base_ns = Namespace("http://example.org/source/sample/")
    md_uri  = URIRef("http://example.org/source/sample/md")

    g = Graph()
    g.bind("dg",  DG)
    g.bind("lis", LIS)
    g.bind("oa",  OA)
    g.bind("ex",  base_ns)

    # Entity 1: Activity with a single supporting quote
    act_uri = URIRef(base_ns["activity/professional-tooth-cleaning"])
    g.add((act_uri, RDF.type, LIS.Activity))
    g.add((act_uri, RDFS.label, Literal("professional tooth cleaning")))
    quote_uri = URIRef(base_ns["quote-aaaa1111"])
    g.add((act_uri, LIS.representedBy, quote_uri))
    g.add((quote_uri, RDF.type, DG.Quote))
    g.add((quote_uri, OA.hasSource, md_uri))
    sel_node = URIRef(base_ns["sel-aaaa1111"])
    g.add((quote_uri, OA.hasSelector, sel_node))
    g.add((sel_node, RDF.type, OA.TextQuoteSelector))
    g.add((sel_node, OA.exact, Literal("professional tooth cleaning rendered on 17.01.2025")))

    # Entity 2: Person
    person_uri = URIRef(base_ns["person/dmitrii-shishkin"])
    g.add((person_uri, RDF.type, LIS.Person))
    g.add((person_uri, RDFS.label, Literal("Dmitrii Shishkin")))

    # Plumbing — should be ignored by find_typed_entities
    file_uri = URIRef(base_ns[""])
    g.add((file_uri, RDF.type, DG.PdfFile))
    g.add((file_uri, RDF.type, DG.File))   # also dg:extractable false

    graphs_dir(tmp_path).mkdir(exist_ok=True)
    g.serialize(destination=str(graphs_dir(tmp_path) / "sample.extract.ttl"), format="turtle")
    return tmp_path


@pytest.fixture
def model():
    return ModelConfig(
        uri=URIRef("http://example.org/model/test"),
        model_id="test-model", label="test", provider="test",
    )


# ── find_typed_entities ────────────────────────────────────────────────────

def test_find_typed_entities_skips_plumbing(part14_project):
    """dg:PdfFile entities (extractable false) should be skipped."""
    g = Graph()
    g.parse(graphs_dir(part14_project) / "sample.extract.ttl", format="turtle")
    ds = build_dataset(part14_project)
    ontology = union_view(ds)

    entities = find_typed_entities(g, ontology)
    labels = {e.label for e in entities}
    assert "professional tooth cleaning" in labels
    assert "Dmitrii Shishkin" in labels
    # File entity should NOT be in there (dg:PdfFile, dg:File are dg:extractable false)
    assert not any(e.uri == URIRef("http://example.org/source/sample/") for e in entities)


def test_find_typed_entities_recovers_evidence(part14_project):
    g = Graph()
    g.parse(graphs_dir(part14_project) / "sample.extract.ttl", format="turtle")
    ds = build_dataset(part14_project)
    ontology = union_view(ds)

    entities = find_typed_entities(g, ontology)
    activity = next(e for e in entities if "tooth cleaning" in e.label)
    assert len(activity.evidence) == 1
    assert "professional tooth cleaning" in activity.evidence[0].exact


# ── refine_types ───────────────────────────────────────────────────────────

def test_refine_types_adds_specific_class(part14_project):
    """RDL hit for 'professional tooth cleaning' → adds wd:Q12345 as additional type."""
    g = Graph()
    g.parse(graphs_dir(part14_project) / "sample.extract.ttl", format="turtle")
    ds = build_dataset(part14_project)
    ontology = union_view(ds)
    entities = find_typed_entities(g, ontology)

    refined_uri = URIRef("http://www.wikidata.org/entity/Q283491")  # tooth cleaning
    resolver = StubResolver({
        "tooth cleaning": ResolutionResult(uri=refined_uri, label="tooth cleaning", confidence=0.9),
    })

    result = refine_types(g, entities, [resolver])
    assert result.new_triples_count == 1

    activity_uri = URIRef("http://example.org/source/sample/activity/professional-tooth-cleaning")
    assert (activity_uri, RDF.type, refined_uri) in g
    # Original LIS type still present (additive)
    assert (activity_uri, RDF.type, LIS.Activity) in g


def test_refine_types_idempotent(part14_project):
    """Running refine_types twice doesn't duplicate triples."""
    g = Graph()
    g.parse(graphs_dir(part14_project) / "sample.extract.ttl", format="turtle")
    ds = build_dataset(part14_project)
    ontology = union_view(ds)
    entities = find_typed_entities(g, ontology)

    refined_uri = URIRef("http://www.wikidata.org/entity/Q283491")
    resolver = StubResolver({
        "tooth cleaning": ResolutionResult(uri=refined_uri, label="cleaning", confidence=0.9),
    })

    first = refine_types(g, entities, [resolver])
    second = refine_types(g, entities, [resolver])
    assert first.new_triples_count == 1
    assert second.new_triples_count == 0   # already there → no-op


def test_refine_types_skips_out_of_scope_entities(part14_project, model):
    """An RDL with a `covers` list declared shouldn't be queried for entities
    outside its scope. POSC's `covers` includes PhysicalObject + UnitOfMeasure
    + Activity etc., but NOT Person/Organization/Location.
    """
    from src.extract_part14.rdl import POSC_CAESAR

    # Make a resolver that uses POSC's covers (industrial scope)
    class _CountingResolver(StubResolver):
        def __init__(self, hits):
            super().__init__(hits)
            # Override to use POSC's scope declarations
            object.__setattr__(self, "config", POSC_CAESAR)

    g = Graph()
    g.parse(graphs_dir(part14_project) / "sample.extract.ttl", format="turtle")
    ds = build_dataset(part14_project)
    ontology = union_view(ds)
    entities = find_typed_entities(g, ontology)

    # If POSC happened to match anything, return a hit — but we expect
    # NO calls for Person entities since Person isn't in POSC's covers.
    resolver = _CountingResolver({
        "tooth cleaning": ResolutionResult(
            uri=URIRef("http://example.org/pca/123"),
            label="Tooth Cleaning", confidence=0.9,
        ),
    })

    # The fixture has Activity + Person entities
    refine_types(g, entities, [resolver], ontology=ontology)

    # Calls should be at most one per entity that IS in POSC's scope
    # (Activity is; Person is not).
    activity_label = "professional tooth cleaning"
    person_label = "Dmitrii Shishkin"
    activity_called = any(activity_label.lower() in c.lower() for c in resolver.calls)
    person_called   = any(person_label.lower() in c.lower() for c in resolver.calls)
    assert activity_called, "Activity should have been queried (in POSC's covers)"
    assert not person_called, "Person should NOT have been queried (out of POSC's scope)"


def test_refine_types_skips_low_confidence(part14_project):
    g = Graph()
    g.parse(graphs_dir(part14_project) / "sample.extract.ttl", format="turtle")
    ds = build_dataset(part14_project)
    ontology = union_view(ds)
    entities = find_typed_entities(g, ontology)

    resolver = StubResolver({
        "tooth": ResolutionResult(
            uri=URIRef("http://www.wikidata.org/entity/Q12345"),
            label="some weak match", confidence=0.3,
        ),
    })
    result = refine_types(g, entities, [resolver], confidence_floor=0.5)
    assert result.new_triples_count == 0


# ── enrich_source end-to-end ──────────────────────────────────────────────

def test_enrich_source_writes_to_separate_enrich_file(part14_project, model):
    """enrich_source writes to <slug>.enrich.ttl, NOT to <slug>.ttl."""
    refined_uri = URIRef("http://www.wikidata.org/entity/Q283491")
    resolver = StubResolver({
        "tooth cleaning": ResolutionResult(uri=refined_uri, label="cleaning", confidence=0.9),
    })
    llm = StubLLM(responses_by_entity={})

    extract_path = graphs_dir(part14_project) / "sample.extract.ttl"
    enrich_path  = graphs_dir(part14_project) / "sample.enrich.ttl"

    extract_before = extract_path.read_text()
    added = enrich_source(
        part14_project, "sample", [resolver],
        client=llm, model=model,
    )
    assert added >= 1

    # Extract layer is untouched
    assert extract_path.read_text() == extract_before, \
        "enrich must not modify the extract output"

    # Enrich file exists and contains the refinement triple
    assert enrich_path.is_file(), "enrich.ttl should be written"
    enrich_g = Graph()
    enrich_g.parse(enrich_path, format="turtle")

    activity_uri = URIRef("http://example.org/source/sample/activity/professional-tooth-cleaning")
    assert (activity_uri, RDF.type, refined_uri) in enrich_g
    # The original LIS type stays in the EXTRACT layer, NOT duplicated in enrich
    assert (activity_uri, RDF.type, LIS.Activity) not in enrich_g

    # The combined view (extract + enrich) has both types
    combined = Graph()
    combined.parse(extract_path, format="turtle")
    combined.parse(enrich_path, format="turtle")
    assert (activity_uri, RDF.type, refined_uri) in combined
    assert (activity_uri, RDF.type, LIS.Activity) in combined


def test_enrich_source_idempotent(part14_project, model):
    """Running enrich twice → second run adds 0 new triples."""
    refined_uri = URIRef("http://www.wikidata.org/entity/Q283491")
    resolver = StubResolver({
        "tooth cleaning": ResolutionResult(uri=refined_uri, label="cleaning", confidence=0.9),
    })
    llm = StubLLM(responses_by_entity={})

    first  = enrich_source(part14_project, "sample", [resolver],
                           client=llm, model=model)
    second = enrich_source(part14_project, "sample", [resolver],
                           client=llm, model=model)
    assert first  >= 1
    assert second == 0


def test_enrich_source_no_match_no_change(part14_project, model):
    """When RDL has no hits for any entity, enrich is a no-op."""
    resolver = StubResolver({})   # no canned hits
    llm = StubLLM(responses_by_entity={})

    added = enrich_source(part14_project, "sample", [resolver],
                          client=llm, model=model)
    assert added == 0
