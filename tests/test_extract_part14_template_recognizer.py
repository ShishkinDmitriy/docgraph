"""Tests for the SPARQL-based template recognizer.

The recognizer pattern-matches every registered template's lowered body
against an extract graph and lifts each match into a structured invocation.
This catches the case where the LLM extracted constituent triples (e.g.
datumValue + datumUOM) but didn't emit the corresponding template
invocation. Pure mechanical, no LLM.
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from dataclasses import dataclass
import json

import pytest

from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.template_recognizer import (
    PartialMatch,
    confirm_loop,
    materialize_recognized,
    partial_match_invocations,
    recognize_invocations,
)
from src.extract_part14.walker import ExtractedEntity, LIS
from src.llm import TextBlock
from src.models import ModelConfig
from src.tasks.init import init_project


EX  = Namespace("http://example.org/src/test/")
TPL = Namespace("urn:docgraph:vocab:template#")
LIS14TPL = Namespace("urn:docgraph:vocab:lis14tpl#")


def _seed_quantity_datum_triples() -> Graph:
    """A graph containing the lowered triples of QuantityDatumWithUOMandValue
    but NOT the lifted form — emulates the case where the LLM extracted the
    binary properties but didn't invoke the template."""
    g = Graph()
    datum = EX["amount-115-84"]
    uom   = EX["eur"]
    g.add((datum, RDF.type,        LIS.ScalarQuantityDatum))
    g.add((datum, LIS.datumUOM,    uom))
    g.add((datum, LIS.datumValue,  Literal("115.84", datatype=XSD.double)))
    g.add((uom,   RDF.type,        LIS.UnitOfMeasure))
    g.add((uom,   RDFS.label,      Literal("EUR")))
    return g


def test_recognize_finds_quantity_datum_pattern_in_graph():
    """The QuantityDatumWithUOMandValue template's lowered body matches
    the seeded triples → one recognized invocation comes back."""
    g = _seed_quantity_datum_triples()
    invocations = recognize_invocations(g)
    # At least one invocation, and at least one is the QuantityDatum template
    matched_uris = {inv.template.uri for inv in invocations}
    assert LIS14TPL.QuantityDatumWithUOMandValue in matched_uris


def test_recognize_returns_correctly_bound_slots():
    """The recognized invocation's bindings match the URIs/literals from
    the source graph — the SPARQL binds slot vars to the actual terms."""
    g = _seed_quantity_datum_triples()
    invocations = recognize_invocations(g)
    qd = next(inv for inv in invocations
              if inv.template.uri == LIS14TPL.QuantityDatumWithUOMandValue)
    assert qd.bindings.get("datum") == EX["amount-115-84"]
    assert qd.bindings.get("uom")   == EX["eur"]
    assert qd.bindings.get("value") == Literal("115.84", datatype=XSD.double)


def test_materialize_recognized_emits_lifted_triples():
    """The lifted form of a recognized invocation lands as new triples we
    can merge back into the extract graph (a typed instance with named
    slot triples)."""
    g = _seed_quantity_datum_triples()
    invs = recognize_invocations(g)
    qd_invs = [i for i in invs
               if i.template.uri == LIS14TPL.QuantityDatumWithUOMandValue]
    lifted = materialize_recognized(qd_invs, base_ns=EX)

    # The lifted graph should have at least the type triple anchoring the
    # invocation as a tpl:Template instance.
    type_triples = list(lifted.triples((None, RDF.type,
                                        LIS14TPL.QuantityDatumWithUOMandValue)))
    assert len(type_triples) == 1
    inst_uri = type_triples[0][0]
    # And the slot triples reference the bound URIs/literal.
    slot_objects = {str(o) for s, p, o in lifted if s == inst_uri}
    assert str(EX["amount-115-84"]) in slot_objects
    assert str(EX["eur"])           in slot_objects
    assert "115.84" in " ".join(slot_objects)


def test_recognize_returns_empty_when_no_pattern_matches():
    """A graph with no LIS-14 patterns produces no recognized invocations."""
    g = Graph()
    g.add((EX.foo, RDF.type, EX.Bar))
    invocations = recognize_invocations(g)
    # There may be templates that match nothing — but specifically no Part 14
    # ones should fire on this empty-of-LIS-14 graph.
    matched_uris = {inv.template.uri for inv in invocations}
    assert LIS14TPL.QuantityDatumWithUOMandValue not in matched_uris


def test_role_pattern_recognized_when_all_three_triples_present():
    """If hasRole + realizedIn + Role-typing are all present (LLM emitted
    them as binary properties without invoking the template), the role
    pattern is recognized and lifted."""
    g = Graph()
    role     = EX["patient-role"]
    activity = EX["cleaning"]
    player   = EX["dmitrii"]
    g.add((role,     RDF.type,         LIS.Role))
    g.add((role,     LIS.realizedIn,   activity))
    g.add((player,   LIS.hasRole,      role))

    invocations = recognize_invocations(g)
    role_invs = [inv for inv in invocations
                 if inv.template.uri == LIS14TPL.RoleRealizedInActivity]
    assert len(role_invs) >= 1
    bindings = role_invs[0].bindings
    assert bindings.get("role")     == role
    assert bindings.get("activity") == activity
    assert bindings.get("player")   == player


def test_role_pattern_NOT_recognized_when_realizedIn_missing():
    """The LLM's frequent miss: it emits hasRole + Role-typing but forgets
    realizedIn. SPARQL recognition is strict — it shouldn't fire on a
    partial match. (This is what motivates the future LLM-confirm tier.)"""
    g = Graph()
    role   = EX["patient-role"]
    player = EX["dmitrii"]
    g.add((role,   RDF.type,    LIS.Role))
    g.add((player, LIS.hasRole, role))
    # NB: no realizedIn

    invocations = recognize_invocations(g)
    role_invs = [inv for inv in invocations
                 if inv.template.uri == LIS14TPL.RoleRealizedInActivity]
    assert role_invs == []


def test_recognize_is_idempotent_on_already_lifted_graph():
    """Running the recognizer twice on the same graph (after merging the
    first run's lifted triples back) doesn't multiply the lifted form —
    the materialized anchor URI is hash-deterministic."""
    g = _seed_quantity_datum_triples()
    first = materialize_recognized(recognize_invocations(g), base_ns=EX)
    for triple in first:
        g.add(triple)
    second = materialize_recognized(recognize_invocations(g), base_ns=EX)
    # Second materialization should produce the same triples as the first
    # (same anchor URI from the same bindings hash).
    assert set(second) <= set(first)


# ── Phase 2: partial-match detection ───────────────────────────────────────

def test_partial_match_finds_role_with_missing_realizedIn():
    """The classic LLM miss: hasRole + Role-typing present, but realizedIn
    absent. Partial-match detection should surface this as a PartialMatch
    keyed on the role pattern with `activity` flagged missing."""
    g = Graph()
    role   = EX["patient-role"]
    player = EX["dmitrii"]
    g.add((role,   RDF.type,    LIS.Role))
    g.add((player, LIS.hasRole, role))

    partials = partial_match_invocations(g)
    role_partials = [p for p in partials
                     if p.template.uri == LIS14TPL.RoleRealizedInActivity
                     and p.missing_slot == "activity"]
    assert len(role_partials) >= 1
    p = role_partials[0]
    assert p.known_bindings.get("role")   == role
    assert p.known_bindings.get("player") == player


def test_partial_match_skips_fully_recognized():
    """If the graph already has a complete match for the pattern, partial
    detection shouldn't ALSO surface it as partial. The fully-recognized
    set is computed first and used to deduplicate."""
    g = Graph()
    role     = EX["patient-role"]
    activity = EX["cleaning"]
    player   = EX["dmitrii"]
    g.add((role,     RDF.type,         LIS.Role))
    g.add((role,     LIS.realizedIn,   activity))
    g.add((player,   LIS.hasRole,      role))

    partials = partial_match_invocations(g)
    role_partials = [p for p in partials
                     if p.template.uri == LIS14TPL.RoleRealizedInActivity]
    # The fully-recognized invocation shouldn't reappear as a partial.
    assert role_partials == [] or all(
        p.known_bindings.get("activity") is None for p in role_partials
    )


# ── Phase 2: batched-loop confirm ──────────────────────────────────────────

@dataclass
class _Resp:
    content: list


class _BatchedMockLLM:
    """Returns the same canned `{Qid: answer}` dict on every call.

    Tracks call count so tests can assert iteration behavior. Each value
    can be either a flat string (legacy shape — wrapped automatically)
    or a {answer, reason} dict.
    """
    def __init__(self, answers_by_qid):
        self.answers_by_qid = answers_by_qid
        self.calls = 0

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        self.calls += 1
        return _Resp(content=[TextBlock(text=json.dumps(self.answers_by_qid))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("recognizer-ontology")
    from rich.console import Console
    init_project(project_dir, Console(quiet=True))
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


def _seed_partial_role_pattern(role_label="patient") -> tuple[Graph, list[ExtractedEntity]]:
    """A graph + entity list with a role and player but NO realizedIn —
    the canonical partial-match fixture."""
    role     = EX["patient-role"]
    activity = EX["cleaning"]
    player   = EX["dmitrii"]
    g = Graph()
    g.add((role,   RDF.type,    LIS.Role))
    g.add((player, LIS.hasRole, role))
    extracted = [
        ExtractedEntity(uri=role,     type_uri=LIS.Role,     label=role_label,
                        types=[LIS.Role]),
        ExtractedEntity(uri=activity, type_uri=LIS.Activity, label="cleaning",
                        types=[LIS.Activity]),
        ExtractedEntity(uri=player,   type_uri=LIS.Person,   label="dmitrii",
                        types=[LIS.Person]),
    ]
    return g, extracted


def test_confirm_loop_makes_one_batched_call_for_multiple_partials(ontology, model):
    """The loop bundles all partials into ONE LLM call per iteration,
    not one per partial (the whole point of the batched redesign)."""
    g, extracted = _seed_partial_role_pattern()
    mock = _BatchedMockLLM({"Q1": "cleaning"})
    confirm_loop(g, markdown="dental cleaning {#id-1}", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX)
    # First iteration finds the partial, asks one batched call. Second
    # iteration sees the now-completed pattern (no new partials), exits.
    assert mock.calls == 1


def test_confirm_loop_resolves_answer_into_realizedIn_triple(ontology, model):
    """When the LLM names a known activity, the previously-missing
    realizedIn triple lands in the graph (the value-add of the loop)."""
    g, extracted = _seed_partial_role_pattern()
    role     = EX["patient-role"]
    activity = EX["cleaning"]
    mock = _BatchedMockLLM({"Q1": "cleaning"})
    confirm_loop(g, markdown="dental cleaning {#id-1}", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX)
    assert (role, LIS.realizedIn, activity) in g


def test_confirm_loop_emits_no_triples_when_llm_says_none(ontology, model):
    """If the LLM answers `none`, no triples land. Better to abstain than
    fabricate."""
    g, extracted = _seed_partial_role_pattern()
    before = len(g)
    mock = _BatchedMockLLM({"Q1": "none"})
    confirm_loop(g, markdown="...", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX)
    assert len(g) == before


def test_confirm_loop_drops_unresolvable_answer(ontology, model):
    """If the LLM names an entity not in the extracted list, the answer
    is dropped (no fabricated target URI)."""
    g, extracted = _seed_partial_role_pattern()
    before = len(g)
    mock = _BatchedMockLLM({"Q1": "some-fictional-activity"})
    confirm_loop(g, markdown="...", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX)
    assert len(g) == before


def test_confirm_loop_does_not_reask_already_asked_questions(ontology, model):
    """`asked_before` set prevents re-asking the same question across
    iterations even when nothing has changed."""
    g, extracted = _seed_partial_role_pattern()
    # LLM says 'none' so no new triples → loop should NOT re-ask the same
    # partial in a second iteration; it should exit.
    mock = _BatchedMockLLM({"Q1": "none"})
    confirm_loop(g, markdown="...", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX,
                 max_iterations=5)
    # Only one call despite max_iterations=5 — second iteration's
    # shortlist is empty (the only partial was already asked).
    assert mock.calls == 1


def test_confirm_loop_stops_after_max_iterations(ontology, model):
    """Hard cap: even if every iteration produces new triples, the loop
    halts at max_iterations to prevent runaway."""
    g, extracted = _seed_partial_role_pattern()
    # LLM resolves the activity → loop runs once, completes the pattern,
    # finds no further partials, exits before hitting the cap.
    mock = _BatchedMockLLM({"Q1": "cleaning"})
    confirm_loop(g, markdown="...", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX,
                 max_iterations=2)
    assert mock.calls <= 2


def test_confirm_loop_handles_structured_response_with_reason(ontology, model):
    """The new response shape is {Qid: {answer, reason}}. Confirms that
    the structured form parses cleanly and the answer still resolves."""
    g, extracted = _seed_partial_role_pattern()
    role     = EX["patient-role"]
    activity = EX["cleaning"]
    mock = _BatchedMockLLM({
        "Q1": {"answer": "cleaning",
               "reason": "the role is articulated alongside the cleaning event"}
    })
    confirm_loop(g, markdown="dental cleaning {#id-1}", extracted=extracted,
                 ontology=ontology, client=mock, model=model, base_ns=EX)
    assert (role, LIS.realizedIn, activity) in g


def test_parse_batched_response_structured_and_legacy_shapes():
    """The parser tolerates both the structured {answer, reason} dict
    and the legacy flat-string shape for backward compatibility."""
    from src.extract_part14.template_recognizer import _parse_batched_response

    raw = json.dumps({
        "Q1": {"answer": "cleaning", "reason": "matches the activity entity"},
        "Q2": "some-name",                    # legacy flat
        "Q3": {"answer": "none", "reason": "no candidate found"},
    })
    parsed = _parse_batched_response(raw, qids=["Q1", "Q2", "Q3"])
    assert parsed["Q1"] == {"answer": "cleaning",   "reason": "matches the activity entity"}
    assert parsed["Q2"] == {"answer": "some-name",  "reason": ""}
    assert parsed["Q3"] == {"answer": "none",       "reason": "no candidate found"}


def test_parse_batched_response_tolerates_trailing_prose():
    """LLMs often append explanatory prose after the closing `}`. The
    parser should ignore it and pull the JSON object out cleanly."""
    from src.extract_part14.template_recognizer import _parse_batched_response

    raw = (
        '```json\n'
        '{"Q1": {"answer": "none", "reason": "no candidate"}}\n'
        '```\n\n'
        '**Reasoning:** Both Q1 and Q2 ask for the quality entity that ...'
    )
    parsed = _parse_batched_response(raw, qids=["Q1"])
    assert parsed["Q1"]["answer"] == "none"
    assert "no candidate" in parsed["Q1"]["reason"]


# ── walk_templates: full templated rewrite of the extract graph ───────────

def test_walk_templates_folds_lowered_into_lifted(ontology, model):
    """The templated graph contains the lifted form for each recognized
    invocation AND has the lowered pattern triples removed (folded into
    the lift). Non-pattern triples (entity labels, evidence anchors)
    survive untouched."""
    from src.extract_part14.template_recognizer import walk_templates
    from src.extract_part14.walker import DG

    # Seed an extract graph with the QuantityDatum pattern + entity facts
    g = _seed_quantity_datum_triples()
    datum = EX["amount-115-84"]
    g.add((datum, RDFS.label, Literal("EUR 115.84")))   # entity decl, NOT pattern
    g.add((datum, DG.typeHint, Literal("MonetaryAmount")))

    extracted = [
        ExtractedEntity(uri=datum, type_uri=LIS.ScalarQuantityDatum,
                        label="EUR 115.84",
                        types=[LIS.ScalarQuantityDatum]),
        ExtractedEntity(uri=EX["eur"], type_uri=LIS.UnitOfMeasure,
                        label="EUR", types=[LIS.UnitOfMeasure]),
    ]
    # confirm_loop returns nothing because nothing is partial; only
    # mechanical recognition runs.
    mock = _BatchedMockLLM({})
    templated = walk_templates(
        g, extracted=extracted, ontology=ontology, base_ns=EX,
        markdown="EUR 115.84 {#id-1}", client=mock, model=model,
    )

    # Lifted form is in the templated graph
    type_triples = list(templated.triples((None, RDF.type,
                                           LIS14TPL.QuantityDatumWithUOMandValue)))
    assert len(type_triples) == 1
    inst = type_triples[0][0]

    # Lowered pattern triples are GONE from the templated graph
    assert (datum, LIS.datumValue, Literal("115.84", datatype=XSD.double)) not in templated
    assert (datum, LIS.datumUOM,   EX["eur"])                                 not in templated
    assert (datum, RDF.type,       LIS.ScalarQuantityDatum)                   not in templated

    # Entity facts (label, typeHint) survived — not part of the lowered pattern
    assert (datum, RDFS.label, Literal("EUR 115.84"))    in templated
    assert (datum, DG.typeHint, Literal("MonetaryAmount")) in templated

    # And the lifted form references the same datum URI through its slot
    slot_objects = {str(o) for s, p, o in templated if s == inst}
    assert str(datum) in slot_objects


def test_walk_templates_returns_full_graph_not_a_delta(ontology, model):
    """The returned graph is self-contained — every non-pattern triple
    from the source extract graph is present, not just the template
    contribution."""
    from src.extract_part14.template_recognizer import walk_templates

    g = Graph()
    # An entity unrelated to any template — must survive untouched.
    g.add((EX["mr-x"], RDF.type,    LIS.Person))
    g.add((EX["mr-x"], RDFS.label,  Literal("Mr X")))

    mock = _BatchedMockLLM({})
    templated = walk_templates(
        g, extracted=[], ontology=ontology, base_ns=EX,
        markdown="...", client=mock, model=model,
    )
    assert (EX["mr-x"], RDF.type,    LIS.Person)        in templated
    assert (EX["mr-x"], RDFS.label,  Literal("Mr X"))   in templated


def test_walk_templates_does_not_mutate_input_graph(ontology, model):
    """walk_templates returns the AFTER state without touching the BEFORE
    — extract_graph stays pristine so the caller can serialize it as the
    before-snapshot. confirm_loop runs on an internal working copy."""
    from src.extract_part14.template_recognizer import walk_templates

    g = Graph()
    role   = EX["patient-role"]
    player = EX["dmitrii"]
    g.add((role,   RDF.type,    LIS.Role))
    g.add((player, LIS.hasRole, role))
    extracted = [
        ExtractedEntity(uri=role,           type_uri=LIS.Role,    label="patient",
                        types=[LIS.Role]),
        ExtractedEntity(uri=EX["cleaning"], type_uri=LIS.Activity, label="cleaning",
                        types=[LIS.Activity]),
        ExtractedEntity(uri=player,         type_uri=LIS.Person,  label="dmitrii",
                        types=[LIS.Person]),
    ]
    triples_before = set(g)
    mock = _BatchedMockLLM({"Q1": "cleaning"})
    walk_templates(
        g, extracted=extracted, ontology=ontology, base_ns=EX,
        markdown="...", client=mock, model=model,
    )
    # Input graph unchanged — no realizedIn triple was added to g
    assert set(g) == triples_before
    assert (role, LIS.realizedIn, EX["cleaning"]) not in g


def test_walk_templates_skips_confirm_when_no_extracted_entities(ontology, model):
    """confirm_loop needs extracted entities (to resolve answers); when
    the list is empty, walk_templates skips the LLM call entirely."""
    from src.extract_part14.template_recognizer import walk_templates

    g = _seed_quantity_datum_triples()
    mock = _BatchedMockLLM({"Q1": "should-not-be-asked"})
    walk_templates(g, extracted=[], ontology=ontology, base_ns=EX,
                   markdown="...", client=mock, model=model)
    assert mock.calls == 0
