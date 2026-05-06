"""Tests for src.templates.expand — substitution and minting semantics."""

from pathlib import Path

import pytest
from rdflib import Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from src.templates.expand import expand
from src.templates.loader import load_template

FIXTURES = Path(__file__).parent / "fixtures" / "templates"

DOM = Namespace("http://example.org/docgraph/financial#")
DG = Namespace("http://example.org/docgraph/meta#")
ISO = Namespace("http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#")
EX = Namespace("http://example.org/x/")
PROV = Namespace("http://www.w3.org/ns/prov#")


def test_passthrough_expansion_emits_one_triple():
    t = load_template(FIXTURES / "passthrough_vat.ttl")

    g = expand(
        t,
        {"invoice": EX["invoice-001"], "value": "DE123456789"},
    )

    triples = list(g)
    assert len(triples) == 1
    s, p, o = triples[0]
    assert s == EX["invoice-001"]
    assert p == DOM.hasVatNumber
    assert o == Literal("DE123456789", datatype=XSD.string)


def test_passthrough_unknown_slot_raises():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    with pytest.raises(ValueError, match="unknown slot"):
        expand(
            t,
            {"invoice": EX["invoice-001"], "value": "X", "bogus": "y"},
        )


def test_passthrough_missing_required_slot_raises():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    with pytest.raises(ValueError, match="missing required slot"):
        expand(t, {"invoice": EX["invoice-001"]})


def test_sourced_assertion_single_reference_produces_full_cluster():
    """One reference → 1 quote node + 1 composition tuple + 1 description
    tuple = 8 triples total (3 on the quote + 3 on composition + 3 on
    description, minus the rdf:type counted only once per subject)."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")

    g = expand(
        t,
        {
            "doc": EX["doc-001"],
            "quoteText": "VAT DE123, issued 2026-04-15",
            "locator": "p.1",
            "references": [EX["invoice-001"]],
        },
    )

    # Exactly one composition tuple, one description tuple, one quote node.
    composition_tuples = list(g.subjects(RDF.type, ISO.CompositionOfIndividual))
    description_tuples = list(g.subjects(RDF.type, ISO.Description))
    quote_nodes = list(g.subjects(RDF.type, DG.Quote))

    assert len(composition_tuples) == 1
    assert len(description_tuples) == 1
    assert len(quote_nodes) == 1

    # Composition links the doc to the quote.
    comp = composition_tuples[0]
    assert (comp, ISO.hasWhole, EX["doc-001"]) in g
    assert (comp, ISO.hasPart, quote_nodes[0]) in g

    # Description links the quote to the reference.
    desc = description_tuples[0]
    assert (desc, ISO.hasSign, quote_nodes[0]) in g
    assert (desc, ISO.hasRepresented, EX["invoice-001"]) in g

    # Quote carries text + locator.
    q = quote_nodes[0]
    assert (q, DG.text, Literal("VAT DE123, issued 2026-04-15", datatype=XSD.string)) in g
    assert (q, DG.locator, Literal("p.1", datatype=XSD.string)) in g

    # 3 (quote) + 3 (composition) + 3 (description) = 9 triples.
    assert len(g) == 9


def test_sourced_assertion_multi_reference_shares_quote_and_composition():
    """Two references → still ONE quote node + ONE composition tuple, but TWO
    description tuples. Connected-component logic correctly identifies the
    composition as not touching the multi-valued slot."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")

    g = expand(
        t,
        {
            "doc": EX["doc-001"],
            "quoteText": "two refs",
            "locator": "p.2",
            "references": [EX["invoice-001"], EX["invoice-002"]],
        },
    )

    composition_tuples = list(g.subjects(RDF.type, ISO.CompositionOfIndividual))
    description_tuples = list(g.subjects(RDF.type, ISO.Description))
    quote_nodes = list(g.subjects(RDF.type, DG.Quote))

    assert len(composition_tuples) == 1, "composition should not duplicate per reference"
    assert len(description_tuples) == 2, "one description per reference"
    assert len(quote_nodes) == 1, "quote should not duplicate per reference"

    # Both descriptions point at the same quote.
    for desc in description_tuples:
        assert (desc, ISO.hasSign, quote_nodes[0]) in g

    # Each description has its own represented target.
    represented = {
        o
        for desc in description_tuples
        for o in g.objects(desc, ISO.hasRepresented)
    }
    assert represented == {EX["invoice-001"], EX["invoice-002"]}


def test_sourced_assertion_uri_minting_is_idempotent():
    """Same bindings → same minted URIs across runs."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    bindings = {
        "doc": EX["doc-001"],
        "quoteText": "x",
        "locator": "p.1",
        "references": [EX["invoice-001"]],
    }

    g1 = expand(t, bindings)
    g2 = expand(t, bindings)

    triples_1 = sorted((str(s), str(p), str(o)) for s, p, o in g1)
    triples_2 = sorted((str(s), str(p), str(o)) for s, p, o in g2)
    assert triples_1 == triples_2


def test_sourced_assertion_different_bindings_give_different_urns():
    t = load_template(FIXTURES / "sourced_assertion.ttl")

    g1 = expand(
        t,
        {"doc": EX["doc-A"], "quoteText": "x", "locator": "p.1",
         "references": [EX["i-1"]]},
    )
    g2 = expand(
        t,
        {"doc": EX["doc-B"], "quoteText": "x", "locator": "p.1",
         "references": [EX["i-1"]]},
    )

    quote_a = list(g1.subjects(RDF.type, DG.Quote))[0]
    quote_b = list(g2.subjects(RDF.type, DG.Quote))[0]
    assert quote_a != quote_b


def test_pattern_form_expansion_single_triple():
    """Pattern-form template — bindings keyed by lifted-graph variable name."""
    t = load_template(FIXTURES / "prov_wgb.ttl")

    g = expand(
        t,
        {"entity": EX["report-1"], "activity": EX["render-job-1"]},
    )

    # Lowered body: one composition tuple wrapping (activity → entity).
    composition_tuples = list(g.subjects(RDF.type, ISO.CompositionOfIndividual))
    assert len(composition_tuples) == 1
    comp = composition_tuples[0]
    assert (comp, ISO.hasWhole, EX["render-job-1"]) in g
    assert (comp, ISO.hasPart, EX["report-1"]) in g
    assert len(g) == 3
