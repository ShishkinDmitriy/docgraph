"""Tests for DocumentAgent._find_entity — no API calls, pure graph lookups."""

from unittest.mock import MagicMock

import pytest
from rdflib import Graph, Literal, URIRef
from rdflib.namespace import FOAF, RDF, RDFS

from src.classifier.agent import DocumentAgent
from src.classifier.models import ModelConfig

FOAF_PERSON = "http://xmlns.com/foaf/0.1/Person"
FOAF_ORG    = "http://xmlns.com/foaf/0.1/Organization"
FOAF_AGENT  = "http://xmlns.com/foaf/0.1/Agent"
FOAF_NAME   = "http://xmlns.com/foaf/0.1/name"
FIN_TAX_ID  = "http://example.org/financial/taxId"

PERSON_URI = URIRef("http://example.org/tax-classifier/dmitrii-shishkin")
ORG_URI    = URIRef("http://example.org/tax-classifier/party_oo-delta")


def _agent(ont_graph: Graph, results_graph: Graph | None = None) -> DocumentAgent:
    model = ModelConfig(
        uri=URIRef("http://example.org/test/model"),
        model_id="test-model",
        label="Test",
    )
    agent = DocumentAgent(
        graph=ont_graph,
        results_graph=results_graph or Graph(),
        client=MagicMock(),
        model=model,
        doc_classes={},
        target_class=URIRef("http://example.org/financial/FinancialDocument"),
    )
    agent._doc_uri = "http://example.org/test/doc"
    return agent


# ── basic lookup ──────────────────────────────────────────────────────────────

def test_find_person_by_name_in_ont_graph():
    """A foaf:Person in the ontology graph is found by foaf:name."""
    g = Graph()
    g.add((PERSON_URI, RDF.type, FOAF.Person))
    g.add((PERSON_URI, FOAF.name, Literal("REDACTED")))

    result = _agent(g)._find_entity("foaf:Person", {"foaf:name": "REDACTED"})

    assert result["matches"], "expected at least one match"
    match = next(m for m in result["matches"] if m["uri"] == str(PERSON_URI))
    assert "known_properties" in match
    assert match["known_properties"].get("foaf:name") == "REDACTED"


def test_find_person_in_results_graph():
    """A foaf:Person that was extracted in a previous document (results_graph) is found."""
    results = Graph()
    results.add((PERSON_URI, RDF.type, FOAF.Person))
    results.add((PERSON_URI, FOAF.name, Literal("REDACTED")))

    result = _agent(Graph(), results)._find_entity("foaf:Person", {"foaf:name": "REDACTED"})

    assert result["matches"], "expected match in results_graph"
    assert any(m["uri"] == str(PERSON_URI) for m in result["matches"])


def test_find_org_by_tax_id():
    """An org can be found by fin:taxId."""
    g = Graph()
    g.add((ORG_URI, RDF.type, FOAF.Organization))
    g.add((ORG_URI, FOAF.name, Literal("ООО Дельта")))
    g.add((ORG_URI, URIRef(FIN_TAX_ID), Literal("7713759202")))

    result = _agent(g)._find_entity("foaf:Organization", {"fin:taxId": "7713759202"})

    assert result["matches"], "expected match by taxId"
    assert any(m["uri"] == str(ORG_URI) for m in result["matches"])


# ── abstract class expansion ──────────────────────────────────────────────────

def test_abstract_agent_expands_to_person():
    """
    Querying with foaf:Agent finds a foaf:Person because _find_entity expands
    to concrete subclasses via rdfs:subClassOf in the ontology graph.
    """
    g = Graph()
    # Ontology declares the hierarchy
    g.add((FOAF.Person,       RDFS.subClassOf, FOAF.Agent))
    g.add((FOAF.Organization, RDFS.subClassOf, FOAF.Agent))
    # The individual is typed as the concrete class
    g.add((PERSON_URI, RDF.type, FOAF.Person))
    g.add((PERSON_URI, FOAF.name, Literal("REDACTED")))

    result = _agent(g)._find_entity("foaf:Agent", {"foaf:name": "REDACTED"})

    assert result["matches"], (
        "foaf:Agent query should match foaf:Person via subclass expansion"
    )
    assert any(m["uri"] == str(PERSON_URI) for m in result["matches"])


# ── no match — suggested URI ──────────────────────────────────────────────────

def test_no_match_returns_suggested_uri():
    """When nothing matches, matches is empty and suggested_uri is a stable string URI."""
    result = _agent(Graph())._find_entity("foaf:Person", {"foaf:name": "Unknown Person"})

    assert result["matches"] == []
    assert result["suggested_uri"].startswith("http")
    # Stable — same input produces the same URI
    result2 = _agent(Graph())._find_entity("foaf:Person", {"foaf:name": "Unknown Person"})
    assert result["suggested_uri"] == result2["suggested_uri"]


def test_suggested_uri_derived_from_name():
    """suggested_uri should embed a slug of the name."""
    result = _agent(Graph())._find_entity("foaf:Person", {"foaf:name": "John Doe"})
    assert "john-doe" in result["suggested_uri"]


# ── integration: real financial_documents.ttl individual ─────────────────────

def test_known_properties_with_multiple_values():
    """When an entity has two addresses (from ont + results graphs), both are returned."""
    ont = Graph()
    ont.add((PERSON_URI, RDF.type, FOAF.Person))
    ont.add((PERSON_URI, FOAF.name, Literal("REDACTED")))
    ont.add((PERSON_URI, FOAF.based_near, Literal("Berlin")))

    results = Graph()
    results.add((PERSON_URI, RDF.type, FOAF.Person))
    results.add((PERSON_URI, FOAF.based_near, Literal("Munich")))  # new value from later doc

    result = _agent(ont, results)._find_entity("foaf:Person", {"foaf:name": "REDACTED"})

    match = next(m for m in result["matches"] if m["uri"] == str(PERSON_URI))
    addresses = match["known_properties"]["foaf:based_near"]
    assert isinstance(addresses, list), "multiple values should be a list"
    assert set(addresses) == {"Berlin", "Munich"}


def test_find_person_in_financial_documents_ttl():
    """
    tax:dmitrii-shishkin from financial_documents.ttl is found when the file is
    loaded into the ontology graph — the same way main.py loads it at runtime.
    """
    from pathlib import Path
    ttl = Path(__file__).parent.parent / "data" / "financial_documents.ttl"
    g = Graph()
    g.parse(ttl)

    agent = _agent(g)

    # Exact-type query
    result = agent._find_entity("foaf:Person", {"foaf:name": "REDACTED"})
    assert result["matches"], (
        "tax:dmitrii-shishkin should be found by foaf:Person + foaf:name"
    )

    # Abstract-type query — works via subclass expansion:
    # foaf:Person rdfs:subClassOf foaf:Agent is declared in the file.
    result_agent = agent._find_entity("foaf:Agent", {"foaf:name": "REDACTED"})
    assert result_agent["matches"], (
        "tax:dmitrii-shishkin should be found when querying with foaf:Agent "
        "(foaf:Person rdfs:subClassOf foaf:Agent is declared in financial_documents.ttl)"
    )
