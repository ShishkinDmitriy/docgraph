"""Tests for the ext-class semantic dedup phase.

walk_dedup() runs in two tiers:
  1. Anchor-scoped cosine pre-filter → top-K shortlist of candidates
     above SHORTLIST_THRESHOLD.
  2. Batched LLM relation classifier → equivalent / subclass / superclass
     / unrelated → per-relation graph mutations.

Tests use:
  - A deterministic fake embedding client (text → unit-basis vector)
    that gives cosine 1.0 for matching first-token keywords, 0.0 for
    different keywords. No OpenAI calls.
  - A fake LLM client that returns a canned `{Qid: {relation, target,
    reason}}` JSON. No Anthropic calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from src.embeddings import EmbeddingStore, DEFAULT_DIM
from src.extract_part14.ext_dedup import (
    RelationDecision,
    embed_text_for_class,
    walk_dedup,
)
from src.extract_part14.ext_ontology import (
    DG,
    EXT,
    LIS,
    ExtClass,
    class_definitions_graph,
)
from src.llm import TextBlock
from src.models import ModelConfig


EX = Namespace("http://example.org/src/test/")


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeEmbeddingClient:
    """Deterministic vectors keyed by the first whitespace-separated token
    of the embed text. Same first token → same vector → cosine 1.0."""
    def __init__(self):
        self.calls       = 0
        self._next_basis = 0
        self._vectors:   dict[str, np.ndarray] = {}

    def _vec_for(self, text: str) -> np.ndarray:
        kw = text.split("\n", 1)[0].strip().split()[0] if text.strip() else ""
        if kw not in self._vectors:
            v = np.zeros(DEFAULT_DIM, dtype=np.float32)
            v[self._next_basis % DEFAULT_DIM] = 1.0
            self._next_basis += 1
            self._vectors[kw] = v
        return self._vectors[kw]

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        return np.asarray([self._vec_for(t) for t in texts], dtype=np.float32)


@dataclass
class _Resp:
    content: list


class _FakeLLM:
    """Returns a canned `{Qid: {relation, target, reason}}` JSON on every
    call. Test asserts what the orchestrator does with it."""
    def __init__(self, answers_by_qid: dict[str, dict]):
        self.answers_by_qid = answers_by_qid
        self.calls = 0

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        self.calls += 1
        return _Resp(content=[TextBlock(text=json.dumps(self.answers_by_qid))])


def _store(tmp_path) -> EmbeddingStore:
    return EmbeddingStore(tmp_path / "embeddings.npz")


def _model() -> ModelConfig:
    return ModelConfig(
        uri=URIRef("http://example.org/model/test"),
        model_id="test-model", label="test", provider="test",
    )


# ── embed_text_for_class ──────────────────────────────────────────────────


def test_embed_text_includes_label_alts_comment():
    cls = ExtClass(
        slug="Invoice", anchor=LIS.InformationObject,
        label="Invoice", alt_labels=["Bill", "Rechnung"],
        comment="A formal billing document.",
    )
    text = embed_text_for_class(cls)
    assert "Invoice" in text
    assert "Bill, Rechnung" in text
    assert "billing document" in text


def test_embed_text_handles_missing_optional_fields():
    cls = ExtClass(slug="Plain", anchor=LIS.InformationObject, label="Plain")
    text = embed_text_for_class(cls)
    assert text == "Plain"


# ── walk_dedup: shortlist + first-doc behavior ────────────────────────────


def test_first_doc_no_existing_classes(tmp_path):
    """First doc in project — no candidates exist. New classes embed + stored."""
    ontology = Graph()
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="Invoice"),
    ]):
        g.add(t)
    g.add((EX["i-1"], RDF.type, EXT.Invoice))

    store = _store(tmp_path)
    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=store, embedding_client=_FakeEmbeddingClient(),
    )
    assert decisions == []
    assert store.has_class(str(EXT.Invoice))


def test_no_candidates_above_shortlist_threshold(tmp_path):
    """Existing class has unrelated keyword (orthogonal vector) → cosine 0.0
    → no shortlist → no LLM call → kept as new."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                 label="invoice document", comment="A bill."),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="DentalChart", anchor=LIS.InformationObject,
                 label="dentalchart record", comment="Clinical record."),
    ]):
        g.add(t)
    g.add((EX["dc-1"], RDF.type, EXT.DentalChart))

    llm = _FakeLLM({})    # would fail loudly if called
    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert decisions == []
    assert llm.calls == 0           # no LLM call needed when shortlist empty
    assert (EX["dc-1"], RDF.type, EXT.DentalChart) in g


def test_anchor_scoping_avoids_cross_kind_collision(tmp_path):
    """Same first token but different anchor → different scope → no compare."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                 label="service charge document"),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="DentalService", anchor=LIS.Activity,
                 label="service performed"),
    ]):
        g.add(t)
    g.add((EX["dental-1"], RDF.type, EXT.DentalService))

    llm = _FakeLLM({})
    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert decisions == []
    assert llm.calls == 0
    assert (EX["dental-1"], RDF.type, EXT.DentalService) in g


# ── walk_dedup: relation = equivalent_to ──────────────────────────────────


def test_equivalent_substitutes_and_enriches_canonical(tmp_path):
    """LLM judges new ext:Bill ≡ ext:Invoice → substitute type triples,
    drop new class definition, enrich canonical with skos:altLabel +
    skos:scopeNote in this doc's graph."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                 label="invoice canonical", comment="Original Invoice comment.",
                 alt_labels=["Rechnung"]),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Bill", anchor=LIS.InformationObject,
                 label="invoice paid",
                 alt_labels=["BillingReceipt"],
                 comment="A bill issued to the customer."),
    ]):
        g.add(t)
    g.add((EX["b-1"], RDF.type, EXT.Bill))

    llm = _FakeLLM({
        "Q1": {"relation": "equivalent_to",
               "target":   "Invoice",
               "reason":   "Same kind — both are billing documents."},
    })
    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert len(decisions) == 1
    assert decisions[0].relation == "equivalent_to"
    assert decisions[0].target.slug == "Invoice"

    # Type rewrite
    assert (EX["b-1"], RDF.type, EXT.Invoice) in g
    assert (EX["b-1"], RDF.type, EXT.Bill)    not in g
    # Audit
    assert (EX["b-1"], DG.proposedAs, Literal("Bill")) in g
    # New class definition dropped
    assert list(g.triples((EXT.Bill, None, None))) == []

    # Enrichment on canonical (in this doc's graph — additive)
    alts_added = {str(o) for o in g.objects(EXT.Invoice, SKOS.altLabel)}
    assert "Bill"             in alts_added   # new class's label became an alt
    assert "BillingReceipt"   in alts_added   # new class's alt_labels propagated
    # New comment lands as scopeNote (additive — doesn't replace canonical's comment)
    notes = {str(o) for o in g.objects(EXT.Invoice, SKOS.scopeNote)}
    assert "A bill issued to the customer." in notes


# ── walk_dedup: relation = subclass_of / superclass_of ────────────────────


def test_subclass_keeps_new_and_adds_subClassOf_link(tmp_path):
    """LLM says new ext:DentalTreatment ⊆ ext:Treatment → keep new class,
    add `<new> rdfs:subClassOf <canonical>` in this doc's graph."""
    ontology = class_definitions_graph([
        ExtClass(slug="Treatment", anchor=LIS.Activity,
                 label="medical treatment", comment="A clinical procedure."),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="DentalTreatment", anchor=LIS.Activity,
                 label="medical procedure on teeth",
                 comment="A treatment specifically of teeth."),
    ]):
        g.add(t)
    g.add((EX["dt-1"], RDF.type, EXT.DentalTreatment))

    llm = _FakeLLM({
        "Q1": {"relation": "subclass_of",
               "target":   "Treatment",
               "reason":   "DentalTreatment is a more specific kind of Treatment."},
    })
    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert len(decisions) == 1
    assert decisions[0].relation == "subclass_of"
    # New class survives (definition + instance)
    assert (EXT.DentalTreatment, RDF.type,         OWL.Class)              in g
    assert (EX["dt-1"],          RDF.type,         EXT.DentalTreatment)    in g
    # Subclass link added
    assert (EXT.DentalTreatment, RDFS.subClassOf, EXT.Treatment)           in g
    # No audit triple — substitution didn't happen
    assert (EX["dt-1"],          DG.proposedAs,    None)                    not in g


def test_superclass_adds_inverse_subClassOf_link(tmp_path):
    """LLM says new ext:Treatment ⊇ ext:DentalTreatment → keep new class,
    add `<canonical> rdfs:subClassOf <new>` in this doc's graph."""
    ontology = class_definitions_graph([
        ExtClass(slug="DentalTreatment", anchor=LIS.Activity,
                 label="medical procedure on teeth"),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Treatment", anchor=LIS.Activity,
                 label="medical procedure"),
    ]):
        g.add(t)
    g.add((EX["t-1"], RDF.type, EXT.Treatment))

    llm = _FakeLLM({
        "Q1": {"relation": "superclass_of",
               "target":   "DentalTreatment",
               "reason":   "Treatment is the more general kind."},
    })
    walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert (EXT.DentalTreatment, RDFS.subClassOf, EXT.Treatment) in g
    # New class also survives
    assert (EXT.Treatment, RDF.type, OWL.Class) in g
    assert (EX["t-1"],     RDF.type, EXT.Treatment) in g


# ── walk_dedup: relation = unrelated, plus LLM hallucinations ─────────────


def test_unrelated_keeps_new_class_as_is(tmp_path):
    """LLM judges unrelated → keep new class definition + instance, no
    subclass link, no substitution."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                 label="invoice canonical"),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Receipt", anchor=LIS.InformationObject,
                 label="invoice acknowledgement"),
    ]):
        g.add(t)
    g.add((EX["r-1"], RDF.type, EXT.Receipt))

    llm = _FakeLLM({
        "Q1": {"relation": "unrelated", "target": "",
               "reason": "Receipt confirms payment; Invoice requests it. Different."},
    })
    walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert (EXT.Receipt,    RDF.type, OWL.Class)   in g
    assert (EX["r-1"],      RDF.type, EXT.Receipt) in g
    # No substitution, no subclass link
    assert (EX["r-1"],      RDF.type, EXT.Invoice) not in g
    assert (EXT.Receipt,    RDFS.subClassOf, EXT.Invoice) not in g


def test_hallucinated_target_downgrades_to_unrelated(tmp_path):
    """If the LLM names a target slug that isn't on the shortlist, the
    decision downgrades to unrelated rather than mis-applying."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                 label="invoice document"),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Bill", anchor=LIS.InformationObject,
                 label="invoice paid"),
    ]):
        g.add(t)
    g.add((EX["b-1"], RDF.type, EXT.Bill))

    llm = _FakeLLM({
        "Q1": {"relation": "equivalent_to",
               "target":   "SomeFictionalClass",
               "reason":   "Invented by the LLM"},
    })
    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    assert decisions[0].relation == "unrelated"
    # New class survives (LLM hallucination didn't trigger substitution)
    assert (EXT.Bill,  RDF.type, OWL.Class) in g
    assert (EX["b-1"], RDF.type, EXT.Bill)  in g
    assert (EX["b-1"], RDF.type, EXT.Invoice) not in g


# ── walk_dedup: also mutates templates graph ──────────────────────────────


def test_decision_applies_to_templates_graph_too(tmp_path):
    """When templates_graph is given, equivalent substitutions and
    subclass links land in BOTH the extract graph and the templates graph."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject,
                 label="invoice canonical"),
    ])
    g = Graph()
    g_t = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Bill", anchor=LIS.InformationObject,
                 label="invoice paid"),
    ]):
        g.add(t)
        g_t.add(t)
    g.add((EX["b-1"],   RDF.type, EXT.Bill))
    g_t.add((EX["b-1"], RDF.type, EXT.Bill))

    llm = _FakeLLM({
        "Q1": {"relation": "equivalent_to", "target": "Invoice", "reason": "same kind"},
    })
    walk_dedup(
        g, g_t, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=llm, llm_model=_model(),
        shortlist_threshold=0.8,
    )
    # Both graphs reflect the substitution
    assert (EX["b-1"], RDF.type, EXT.Invoice) in g
    assert (EX["b-1"], RDF.type, EXT.Invoice) in g_t


# ── walk_dedup: legacy auto-substitute path (no LLM client) ──────────────


def test_legacy_no_llm_falls_back_to_auto_substitute(tmp_path):
    """Some callers (older tests, scripts) may invoke walk_dedup without
    an LLM client. In that case, auto-substitute on cosine ≥ 0.88
    (legacy behavior) instead of asking for relations."""
    ontology = class_definitions_graph([
        ExtClass(slug="Invoice", anchor=LIS.InformationObject, label="invoice doc"),
    ])
    g = Graph()
    for t in class_definitions_graph([
        ExtClass(slug="Bill", anchor=LIS.InformationObject, label="invoice paid"),
    ]):
        g.add(t)
    g.add((EX["b-1"], RDF.type, EXT.Bill))

    decisions = walk_dedup(
        g, None, ontology=ontology,
        embedding_store=_store(tmp_path),
        embedding_client=_FakeEmbeddingClient(),
        llm_client=None,    # legacy path
        shortlist_threshold=0.8,
    )
    # Cosine is 1.0 (matched first token "invoice") ≥ 0.88 → auto-equivalent
    assert len(decisions) == 1
    assert decisions[0].relation == "equivalent_to"
    assert (EX["b-1"], RDF.type, EXT.Invoice) in g
