"""M2 stage 1 — walker tests with a mock LLM.

Verifies that:
- Branches are walked in order, derived from the loaded ontology
- Entities mint deterministic URIs from (branch_label, entity_name)
- Quotes mint with oa:TextQuoteSelector and content-hashed URIs
- Quote dedup works (same exact text → same URI)
- Disjointness exclusion: entities of class A are not re-extracted in
  branch B when A ⊥ B
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from rdflib import Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from src.extract_part14 import axioms
from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.walker import (
    DG,
    LIS,
    OA,
    EvidenceSelector,
    ExtractedEntity,
    _format_existing,
    _sort_by_specificity,
    mint_quote,
    walk_branches,
    walk_stage1,
)
from src.llm import TextBlock
from src.models import ModelConfig
from src.project import init_project, PIPELINE_PART14


# ── Mock infrastructure ────────────────────────────────────────────────────

@dataclass
class _MockResp:
    content: list


class MockLLMClient:
    """Returns canned responses keyed by which class is being extracted."""
    def __init__(self, responses_by_class: dict[str, list[dict]]):
        self.responses_by_class = responses_by_class
        self.calls: list[str] = []

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        prompt = messages[0]["content"] if messages else ""
        # Find which class this prompt is asking about (look for the
        # `instances of "X"` substring)
        class_label = None
        for label in self.responses_by_class:
            if f'instances of "{label}"' in prompt:
                class_label = label
                break
        self.calls.append(class_label or "(unknown)")
        if class_label is None:
            payload = {"instances": []}
        else:
            payload = {"instances": self.responses_by_class[class_label]}
        return _MockResp(content=[TextBlock(text=json.dumps(payload))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("walker-ontology")
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


# ── Quote minting ──────────────────────────────────────────────────────────

def test_mint_quote_emits_oa_textquoteselector():
    from rdflib import Graph
    g = Graph()
    base_ns = Namespace("http://example.org/source/x/")
    md_uri = URIRef("http://example.org/source/x/md")

    sel = EvidenceSelector(exact="hello world", prefix="say ", suffix=" today")
    q_uri = mint_quote(g, sel, base_ns=base_ns, md_source_uri=md_uri)

    assert (q_uri, RDF.type, DG.Quote) in g
    assert (q_uri, RDF.type, LIS.InformationObject) in g
    assert (q_uri, OA.hasSource, md_uri) in g

    selectors = list(g.objects(q_uri, OA.hasSelector))
    assert len(selectors) == 1
    sel_node = selectors[0]
    assert (sel_node, RDF.type, OA.TextQuoteSelector) in g
    assert (sel_node, OA.exact, None) in [(s, p, None) for s, p, o in g.triples((sel_node, OA.exact, None))]


def test_quote_dedup_same_text():
    from rdflib import Graph
    g = Graph()
    base_ns = Namespace("http://example.org/source/x/")
    md_uri = URIRef("http://example.org/source/x/md")

    sel1 = EvidenceSelector(exact="identical text")
    sel2 = EvidenceSelector(exact="identical text", prefix="(different prefix)")
    q1 = mint_quote(g, sel1, base_ns=base_ns, md_source_uri=md_uri)
    q2 = mint_quote(g, sel2, base_ns=base_ns, md_source_uri=md_uri)
    assert q1 == q2


# ── Stage 1 walker ─────────────────────────────────────────────────────────

def test_walk_stage1_extracts_per_branch(ontology, model):
    """Mock LLM returns one Activity entity in the Activity branch."""
    mock = MockLLMClient(responses_by_class={
        "Activity": [
            {"name": "professional dental cleaning",
             "evidence": [{"exact": "professional dental cleaning on 2025-01-17",
                           "prefix": "for ", "suffix": ", patient"}]},
        ],
        # All other branches → empty
    })

    base_ns = Namespace("http://example.org/source/test/")
    md_uri  = URIRef("http://example.org/source/test/md")

    g, extracted = walk_stage1(
        full_markdown="some markdown",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    # 10 branches for POSC's LIS-14 with recursive descent:
    # Activity stays (2 children — under threshold)
    # Aspect stays (2 children: Quality, RealizableEntity — under threshold)
    # Object descends → FunctionalObject, InformationObject, Location, Organization
    # PhysicalObject (Object's 5th child) descends further → Compound, Feature,
    #     InanimatePhysicalObject, Organism
    # Total: 1 (Activity) + 1 (Aspect) + 4 (Object's other children) + 4
    #        (PhysicalObject's children) = 10
    assert len(mock.calls) == 10
    assert "Activity" in mock.calls

    # One Activity entity extracted
    assert len(extracted) == 1
    e = extracted[0]
    assert e.type_uri == LIS.Activity
    assert e.label == "professional dental cleaning"

    # Entity carries lis:representedBy to its quote
    quotes = list(g.objects(e.uri, LIS.representedBy))
    assert len(quotes) == 1
    quote = quotes[0]
    assert (quote, RDF.type, DG.Quote) in g

    # Quote URI is content-hashed
    assert "quote-" in str(quote)


def test_walk_stage1_disjointness_excludes_already_typed(ontology, model):
    """Entities found in the Activity branch are excluded from Object/Aspect
    branches (because Activity ⊥ Aspect ⊥ Object)."""
    captured_prompts: list[str] = []

    class CapturingMock(MockLLMClient):
        def create(self, *, messages, **kwargs):
            captured_prompts.append(messages[0]["content"])
            return super().create(messages=messages, **kwargs)

    mock = CapturingMock(responses_by_class={
        "Activity": [
            {"name": "ent-A1",
             "evidence": [{"exact": "First activity"}]},
        ],
        # All other branches return empty (default behavior of MockLLMClient)
    })

    base_ns = Namespace("http://example.org/source/y/")
    md_uri  = URIRef("http://example.org/source/y/md")

    g, extracted = walk_stage1(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    assert len(extracted) == 1
    # The second and third prompts (Aspect, Object) should mention the
    # Activity-typed entity in the "Excluded" block
    assert any("ent-A1" in p and "Excluded" in p for p in captured_prompts[1:])


def test_walk_stage1_quote_text_dedup_across_entities(ontology, model):
    """Two entities citing the same quote text get the same quote URI."""
    mock = MockLLMClient(responses_by_class={
        "Activity": [
            {"name": "act-1",
             "evidence": [{"exact": "shared paragraph"}]},
        ],
        # Pick Organization (an Object child, since Object descends in
        # effective_branches with default threshold)
        "Organization": [
            {"name": "org-1",
             "evidence": [{"exact": "shared paragraph"}]},
        ],
    })

    base_ns = Namespace("http://example.org/source/z/")
    md_uri  = URIRef("http://example.org/source/z/md")

    g, extracted = walk_stage1(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    # Both entities should reference the same quote URI
    assert len(extracted) == 2
    quotes_a = list(g.objects(extracted[0].uri, LIS.representedBy))
    quotes_b = list(g.objects(extracted[1].uri, LIS.representedBy))
    assert quotes_a == quotes_b


# ── Specificity ordering ───────────────────────────────────────────────────

def test_sort_by_specificity_subclass_before_parent(ontology):
    """ScalarQuantityDatum (a subclass of QuantityDatum) sorts BEFORE
    QuantityDatum so when the parent runs, the subclass entities are already
    in the 'Already extracted' block."""
    branches = [LIS.QuantityDatum, LIS.ScalarQuantityDatum]
    ordered = _sort_by_specificity(branches, ontology)
    assert ordered.index(LIS.ScalarQuantityDatum) < ordered.index(LIS.QuantityDatum)


def test_sort_by_specificity_unrelated_branches_preserve_input_order(ontology):
    """Sibling/unrelated branches preserve caller's input order — only
    subclass relationships override it."""
    branches = [LIS.Person, LIS.Organization, LIS.Activity]
    ordered = _sort_by_specificity(branches, ontology)
    # All three have 0 ancestors in the set → input order preserved
    assert ordered == branches


def test_sort_by_specificity_only_counts_ancestors_in_set(ontology):
    """Person has many ancestors in the ontology, but if none of them are in
    the input branches, Person isn't pulled toward the front by their absence."""
    # Person + ScalarQuantityDatum: neither is an ancestor of the other.
    # Person's ancestors (Organism, etc.) are NOT in the input set, so it
    # gets ancestor-count 0. SQD's ancestors (QuantityDatum) is also NOT in
    # the input → both get 0 → input order preserved.
    branches = [LIS.Person, LIS.ScalarQuantityDatum]
    ordered = _sort_by_specificity(branches, ontology)
    assert ordered == branches


# ── _format_existing — subclass-vs-other split ─────────────────────────────

def test_format_existing_marks_subclass_entries_distinctly(ontology):
    """When an extracted entity is typed at a subclass of the current branch,
    the prompt block calls it out as MORE SPECIFIC and uses stronger language."""
    sqd_entity = ExtractedEntity(
        uri      = URIRef("http://x/scalarquantitydatum/sqd-1"),
        type_uri = LIS.ScalarQuantityDatum,
        label    = "invoice total 115.84 EUR",
    )
    person_entity = ExtractedEntity(
        uri      = URIRef("http://x/person/p-1"),
        type_uri = LIS.Person,
        label    = "Dmitrii Shishkin",
    )

    # current branch is QuantityDatum (parent of ScalarQuantityDatum).
    text = _format_existing([sqd_entity, person_entity], LIS.QuantityDatum, ontology)

    assert "MORE SPECIFIC subclasses" in text
    assert "invoice total 115.84 EUR" in text
    # Person is NOT a subclass of QuantityDatum → goes in the other section
    assert "compatible types" in text
    assert "Dmitrii Shishkin" in text


def test_format_existing_no_subclass_section_when_none(ontology):
    """If none of the entities are subclasses of the current branch, the
    'MORE SPECIFIC' header doesn't appear."""
    person = ExtractedEntity(
        uri=URIRef("http://x/p"), type_uri=LIS.Person, label="P",
    )
    text = _format_existing([person], LIS.Activity, ontology)
    assert "MORE SPECIFIC" not in text
    assert "compatible types" in text


# ── End-to-end: ordering + exclusion list ──────────────────────────────────

def test_walk_branches_processes_subclass_before_parent(ontology, model):
    """Mock LLM returns 'invoice fee 115.84' for ScalarQuantityDatum.
    QuantityDatum's prompt should then show that entity in the
    'MORE SPECIFIC subclasses' section."""
    captured: list[tuple[str, str]] = []   # (class_label, prompt)

    class CapturingMock(MockLLMClient):
        def create(self, *, model_id, messages, **kwargs):
            prompt = messages[0]["content"] if messages else ""
            class_label = None
            for label in self.responses_by_class:
                if f'instances of "{label}"' in prompt:
                    class_label = label
                    break
            captured.append((class_label or "(unknown)", prompt))
            return super().create(model_id=model_id, messages=messages, **kwargs)

    mock = CapturingMock(responses_by_class={
        "ScalarQuantityDatum": [
            {"name": "invoice fee 115.84",
             "evidence": [{"exact": "EUR 115,84"}]},
        ],
        "QuantityDatum": [],   # parent returns nothing — but its prompt is what we inspect
    })

    base_ns = Namespace("http://example.org/source/spec/")
    md_uri  = URIRef("http://example.org/source/spec/md")

    g, extracted, _deferred = walk_branches(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
        branches=[LIS.QuantityDatum, LIS.ScalarQuantityDatum],   # input order: parent first
    )

    # SQD must be processed before QuantityDatum regardless of input order.
    classes_called_in_order = [label for label, _ in captured]
    assert classes_called_in_order.index("ScalarQuantityDatum") < \
           classes_called_in_order.index("QuantityDatum")

    # The QuantityDatum prompt must list the SQD entity under MORE SPECIFIC.
    qd_prompt = next(p for label, p in captured if label == "QuantityDatum")
    assert "MORE SPECIFIC subclasses" in qd_prompt
    assert "invoice fee 115.84" in qd_prompt
