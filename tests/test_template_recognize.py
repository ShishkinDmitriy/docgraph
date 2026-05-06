"""Tests for src.templates.recognize — lowered → lifted matching.

Storage graphs are built from inline TTL strings rather than via `expand` so
the data the recognizer is matching is visible alongside the assertions. One
test asserts on the generated SPARQL itself so a reviewer can see the query
the translator produces.
"""

from pathlib import Path

import pytest
from rdflib import Graph, Literal, Namespace
from rdflib.namespace import XSD

from src.templates.expand import expand
from src.templates.loader import load_template
from src.templates.recognize import recognize, to_sparql

FIXTURES = Path(__file__).parent / "fixtures" / "templates"

EX = Namespace("http://example.org/x/")
DOM = Namespace("http://example.org/docgraph/financial#")

PREFIXES = """\
@prefix dom:      <http://example.org/docgraph/financial#> .
@prefix dg:       <http://example.org/docgraph/meta#> .
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix prov:     <http://www.w3.org/ns/prov#> .
@prefix ex:       <http://example.org/x/> .
@prefix xsd:      <http://www.w3.org/2001/XMLSchema#> .
"""


def _graph(ttl_body: str) -> Graph:
    """Parse PREFIXES + ttl_body into a Graph."""
    g = Graph()
    g.parse(data=PREFIXES + ttl_body, format="turtle")
    return g


# ---------------------------------------------------------------------------
# 1. The translator's output — compared against per-template golden files.
#    Each `<stem>.ttl` template fixture has a sibling `<stem>.sparql` file
#    holding the expected query. Regenerate after intentional translator
#    changes with:
#        python -c "from src.templates import load_template, to_sparql; \
#            from pathlib import Path; F=Path('tests/fixtures/templates'); \
#            [(F/f'{s}.sparql').write_text(to_sparql(load_template(F/f'{s}.ttl'))+'\n') \
#             for s in ('passthrough_vat','sourced_assertion','prov_wgb')]"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "stem", ["passthrough_vat", "sourced_assertion", "pdf_converted_to_markdown"]
)
def test_to_sparql_matches_golden_fixture(stem):
    template = load_template(FIXTURES / f"{stem}.ttl")
    expected = (FIXTURES / f"{stem}.sparql").read_text(encoding="utf-8")
    actual = to_sparql(template) + "\n"
    assert actual == expected


# ---------------------------------------------------------------------------
# 2. Recognition against hand-authored storage graphs.
# ---------------------------------------------------------------------------

def test_passthrough_recognizes_one_invoice():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    storage = _graph(
        """
        ex:invoice-001 dom:hasVatNumber "DE123456789" .
        """
    )

    matches = recognize(t, storage)
    assert matches == [
        {"invoice": EX["invoice-001"], "value": Literal("DE123456789")}
    ]


def test_passthrough_no_match_in_unrelated_graph():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    storage = _graph("ex:x ex:unrelated ex:y .")
    assert recognize(t, storage) == []


def test_passthrough_recognizes_multiple_independent_invoices():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    storage = _graph(
        """
        ex:i-1 dom:hasVatNumber "DE111" .
        ex:i-2 dom:hasVatNumber "DE222" .
        """
    )

    matches = recognize(t, storage)
    invoices = {m["invoice"] for m in matches}
    assert invoices == {EX["i-1"], EX["i-2"]}
    assert len(matches) == 2


def test_sourced_assertion_recognizes_single_reference():
    """Hand-authored storage with one quote chain + one Description tuple."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    storage = _graph(
        """
        ex:q a dg:Quote ;
             dg:text "VAT DE123" ;
             dg:locator "p.1" .

        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:doc-001 ;
          iso15926:hasPart  ex:q ] .

        [ a iso15926:Description ;
          iso15926:hasSign        ex:q ;
          iso15926:hasRepresented ex:invoice-001 ] .
        """
    )

    matches = recognize(t, storage)
    assert len(matches) == 1
    m = matches[0]
    assert m["doc"] == EX["doc-001"]
    assert m["quoteText"] == Literal("VAT DE123")
    assert m["locator"] == Literal("p.1")
    assert m["references"] == [EX["invoice-001"]]


def test_sourced_assertion_folds_multiple_descriptions_into_one_instance():
    """One quote shared by two Description tuples → one instance with a
    references list of length 2."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    storage = _graph(
        """
        ex:q a dg:Quote ;
             dg:text "two refs" ;
             dg:locator "p.2" .

        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:doc-001 ;
          iso15926:hasPart  ex:q ] .

        [ a iso15926:Description ;
          iso15926:hasSign        ex:q ;
          iso15926:hasRepresented ex:invoice-001 ] .

        [ a iso15926:Description ;
          iso15926:hasSign        ex:q ;
          iso15926:hasRepresented ex:invoice-002 ] .
        """
    )

    matches = recognize(t, storage)
    assert len(matches) == 1
    m = matches[0]
    assert m["doc"] == EX["doc-001"]
    # Multi-valued slot: SPARQL has no inherent ordering across rows.
    assert set(m["references"]) == {EX["invoice-001"], EX["invoice-002"]}


def test_sourced_assertion_distinguishes_two_independent_assertions():
    """Two complete sourced-assertions in one graph (different doc + quote
    + composition + description chains) → two recognized instances."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    storage = _graph(
        """
        # Assertion A: doc-A, quote qA, two references.
        ex:qA a dg:Quote ; dg:text "qA" ; dg:locator "p.1" .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:doc-A ; iso15926:hasPart ex:qA ] .
        [ a iso15926:Description ;
          iso15926:hasSign ex:qA ; iso15926:hasRepresented ex:i-1 ] .
        [ a iso15926:Description ;
          iso15926:hasSign ex:qA ; iso15926:hasRepresented ex:i-2 ] .

        # Assertion B: doc-B, quote qB, one reference.
        ex:qB a dg:Quote ; dg:text "qB" ; dg:locator "p.9" .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:doc-B ; iso15926:hasPart ex:qB ] .
        [ a iso15926:Description ;
          iso15926:hasSign ex:qB ; iso15926:hasRepresented ex:i-3 ] .
        """
    )

    by_doc = {m["doc"]: m for m in recognize(t, storage)}
    assert set(by_doc) == {EX["doc-A"], EX["doc-B"]}
    assert set(by_doc[EX["doc-A"]]["references"]) == {EX["i-1"], EX["i-2"]}
    assert by_doc[EX["doc-A"]]["quoteText"] == Literal("qA")
    assert set(by_doc[EX["doc-B"]]["references"]) == {EX["i-3"]}
    assert by_doc[EX["doc-B"]]["quoteText"] == Literal("qB")


def test_sourced_assertion_no_match_when_description_tuple_missing():
    """Quote and composition present but no description → required structure
    incomplete; no recognition."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    storage = _graph(
        """
        ex:q a dg:Quote ; dg:text "x" ; dg:locator "p.1" .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:doc-A ; iso15926:hasPart ex:q ] .
        """
    )
    assert recognize(t, storage) == []


def test_pattern_form_recognizes_pdf_conversion():
    """Pattern-form bridge: source/target/activity bindings from a full conversion cluster."""
    t = load_template(FIXTURES / "pdf_converted_to_markdown.ttl")
    storage = _graph(
        """
        ex:conv-1   a iso15926:Activity .
        ex:pdf-1    a dg:PdfFile .
        ex:md-1     a dg:MarkdownFile .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:conv-1 ;
          iso15926:hasPart  ex:pdf-1 ] .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:conv-1 ;
          iso15926:hasPart  ex:md-1 ] .
        """
    )

    matches = recognize(t, storage)
    assert len(matches) == 1
    m = matches[0]
    assert m["source"] == EX["pdf-1"]
    assert m["target"] == EX["md-1"]
    assert m["activity"] == EX["conv-1"]


def test_pattern_form_pdf_conversion_not_matched_by_generic_composition():
    """A composition cluster without PdfFile/MarkdownFile typing must not match —
    the document types are the distinguishing Part 2 semantics."""
    t = load_template(FIXTURES / "pdf_converted_to_markdown.ttl")
    storage = _graph(
        """
        ex:conv-1 a iso15926:Activity .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:conv-1 ;
          iso15926:hasPart  ex:thing-A ] .
        [ a iso15926:CompositionOfIndividual ;
          iso15926:hasWhole ex:conv-1 ;
          iso15926:hasPart  ex:thing-B ] .
        """
    )

    assert recognize(t, storage) == []


# ---------------------------------------------------------------------------
# 3. End-to-end smoke test: expand → recognize round-trip.
# ---------------------------------------------------------------------------

def test_expand_recognize_round_trip_passthrough():
    """If we expand a template instance and feed the result back to
    recognize, we should recover the same bindings."""
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    bindings = {"invoice": EX["invoice-001"], "value": "DE123456789"}

    matches = recognize(t, expand(t, bindings))
    assert len(matches) == 1
    assert matches[0]["invoice"] == EX["invoice-001"]
    assert matches[0]["value"] == Literal(
        "DE123456789", datatype=XSD.string
    )
