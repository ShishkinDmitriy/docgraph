"""Combined per-branch extraction + late resolution tests with mock LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.property_walker import resolve_deferred_references
from src.extract_part14.walker import (
    DG,
    LIS,
    DeferredReference,
    EvidenceSelector,
    ExtractedEntity,
    walk_branches,
)
from src.llm import TextBlock
from src.models import ModelConfig
from src.project import init_project, PIPELINE_PART14


@dataclass
class _MockResp:
    content: list


class MockBranchLLM:
    """Mock for the combined per-branch LLM call. Returns canned instances
    (with optional properties) keyed by class label."""
    def __init__(self, responses: dict[str, list[dict]]):
        self.responses = responses
        self.calls: list[str] = []

    def create(self, *, model_id, messages, **_):
        prompt = messages[0]["content"] if messages else ""
        self.calls.append(prompt)
        for class_label, instances in self.responses.items():
            if f'instances of "{class_label}"' in prompt:
                payload = {"instances": instances}
                return _MockResp(content=[TextBlock(text=json.dumps(payload))])
        return _MockResp(content=[TextBlock(text=json.dumps({"instances": []}))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("combined-ontology")
    from rich.console import Console
    init_project(project_dir, Console(quiet=True), pipeline=PIPELINE_PART14)
    ds = build_dataset(project_dir)
    return union_view(ds)


@pytest.fixture
def model():
    return ModelConfig(
        uri=URIRef("http://example.org/model/test"),
        model_id="test-model", label="test", provider="test",
    )


# ── Combined walker emits literal property values immediately ──────────────

def test_walker_extracts_entity_with_literal_property(ontology, model):
    mock = MockBranchLLM(responses={
        "Activity": [
            {
                "name": "Professional cleaning",
                "evidence": [{"exact": "Professional cleaning on 17.01.2025",
                              "prefix": "", "suffix": ""}],
                "properties": [
                    # No literal-typed property exists for Activity in LIS-14
                    # (creates / hasParticipant / hasActivityPart / occursRelativeTo
                    # are all object-typed) — so this test focuses on the entity
                    # extraction path with empty property list.
                ],
            }
        ],
    })

    base_ns = Namespace("http://example.org/source/x/")
    md_uri  = URIRef("http://example.org/source/x/md")
    branches = [URIRef(str(LIS) + "Activity")]

    g, extracted, deferred = walk_branches(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
        branches=branches,
    )

    assert len(extracted) == 1
    assert extracted[0].label == "Professional cleaning"
    assert extracted[0].type_uri == LIS.Activity
    assert deferred == []


# ── Cross-entity reference: deferred when target not yet extracted ─────────

def test_walker_defers_unresolved_value_entity(ontology, model):
    """Activity branch runs first and references 'Polina Liebermann' as a
    participant; Person branch hasn't run yet → deferred."""
    mock = MockBranchLLM(responses={
        "Activity": [
            {
                "name": "Professional cleaning",
                "evidence": [{"exact": "cleaning on 17.01.2025"}],
                "properties": [
                    {
                        "property": "lis:hasParticipant",
                        "value_entity": "Polina Liebermann",
                        "evidence": "Polina (dentist)",
                    },
                ],
            }
        ],
    })

    base_ns = Namespace("http://example.org/source/y/")
    md_uri  = URIRef("http://example.org/source/y/md")
    branches = [URIRef(str(LIS) + "Activity")]   # ONLY Activity, no Person

    g, extracted, deferred = walk_branches(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
        branches=branches,
    )

    assert len(extracted) == 1
    assert len(deferred) == 1
    assert deferred[0].name == "Polina Liebermann"
    assert deferred[0].predicate == URIRef(str(LIS) + "hasParticipant")
    # No triple bound yet for the participant
    assert (extracted[0].uri, URIRef(str(LIS) + "hasParticipant"), None) not in [
        (s, p, None) for s, p, o in g.triples((extracted[0].uri, URIRef(str(LIS) + "hasParticipant"), None))
    ]


# ── Cross-entity reference: resolved immediately if target already known ───

def test_walker_resolves_value_entity_immediately(ontology, model):
    """If the LLM cites an entity that's already in `existing` (from a prior
    branch), the URI binds in this same branch's pass — no deferred ref."""
    base_ns = Namespace("http://example.org/source/z/")
    md_uri  = URIRef("http://example.org/source/z/md")

    # Two branches: Person first, then Activity references the Person
    mock = MockBranchLLM(responses={
        "Person": [
            {
                "name": "Polina",
                "evidence": [{"exact": "Polina (dentist)"}],
                "properties": [],
            }
        ],
        "Activity": [
            {
                "name": "Cleaning",
                "evidence": [{"exact": "cleaning"}],
                "properties": [
                    {
                        "property": "lis:hasParticipant",
                        "value_entity": "Polina",
                        "evidence": "performed by Polina",
                    },
                ],
            }
        ],
    })

    branches = [URIRef(str(LIS) + "Person"), URIRef(str(LIS) + "Activity")]
    g, extracted, deferred = walk_branches(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
        branches=branches,
    )

    polina = next(e for e in extracted if e.label == "Polina")
    cleaning = next(e for e in extracted if e.label == "Cleaning")

    # No deferred — Polina was already extracted when Activity ran
    assert deferred == []
    # The hasParticipant triple exists with Polina's URI
    assert (cleaning.uri, URIRef(str(LIS) + "hasParticipant"), polina.uri) in g


# ── Late resolution pass ───────────────────────────────────────────────────

def test_resolve_deferred_binds_when_name_matches():
    """If a deferred ref's name matches an entity discovered in a later
    branch, the resolution pass binds it."""
    polina = ExtractedEntity(
        uri=URIRef("ex:person/polina"),
        type_uri=LIS.Person,
        label="Polina Liebermann",
    )
    cleaning_uri = URIRef("ex:activity/cleaning")
    deferred = [DeferredReference(
        subject=cleaning_uri,
        predicate=URIRef(str(LIS) + "hasParticipant"),
        name="Polina Liebermann",
    )]

    g = resolve_deferred_references(
        deferred, extracted_entities=[polina],
    )
    assert (cleaning_uri, URIRef(str(LIS) + "hasParticipant"), polina.uri) in g


def test_resolve_deferred_falls_back_to_literal():
    """Unmatched name → emit as a plain Literal so the value isn't lost."""
    cleaning_uri = URIRef("ex:activity/cleaning")
    deferred = [DeferredReference(
        subject=cleaning_uri,
        predicate=URIRef(str(LIS) + "hasParticipant"),
        name="Some Unknown Person",
    )]

    g = resolve_deferred_references(deferred, extracted_entities=[])
    triples = list(g.triples((cleaning_uri, URIRef(str(LIS) + "hasParticipant"), None)))
    assert len(triples) == 1
    assert isinstance(triples[0][2], Literal)
    assert str(triples[0][2]) == "Some Unknown Person"
