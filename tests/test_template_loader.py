"""Tests for src.templates.loader — pure parsing, no expansion."""

from pathlib import Path

import pytest
from rdflib import Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from src.templates.loader import (
    TPL,
    load_template,
    slot_predicate,
    slug_from_template_uri,
)

FIXTURES = Path(__file__).parent / "fixtures" / "templates"

DOM = Namespace("http://example.org/docgraph/financial#")
DG = Namespace("http://example.org/docgraph/meta#")
ISO = Namespace("http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#")
PROV = Namespace("http://www.w3.org/ns/prov#")


def test_slug_pascal_case_to_kebab():
    assert slug_from_template_uri(URIRef("http://x/SourcedAssertion")) == \
        "sourced-assertion"
    assert slug_from_template_uri(URIRef("http://x/InvoiceHasVatNumber")) == \
        "invoice-has-vat-number"
    assert slug_from_template_uri(URIRef("http://x#SourcedAssertion")) == \
        "sourced-assertion"
    # Already-kebab names pass through unchanged.
    assert slug_from_template_uri(URIRef("urn:tpl/prov-wgb")) == "prov-wgb"


def test_passthrough_template_loads_with_two_slots():
    t = load_template(FIXTURES / "passthrough_vat.ttl")

    assert t.uri == DOM.InvoiceHasVatNumber
    assert t.is_instance_form is True
    assert t.slug == "invoice-has-vat-number"
    assert {s.name for s in t.slots} == {"invoice", "value"}

    invoice = t.slot("invoice")
    value = t.slot("value")
    assert invoice.range == DOM.Invoice
    assert invoice.is_literal is False
    assert value.range == XSD.string
    assert value.is_literal is True


def test_passthrough_lifted_is_auto_derived():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    var = t.var_ns

    # Slot predicates live in a slug-based namespace, not under the template URI
    # (template URIs may already contain a `#`).
    assert slot_predicate(t.slug, "invoice") == URIRef(
        "urn:tpl/invoice-has-vat-number/slot/invoice"
    )

    # var:this rdf:type <template-uri>
    assert (var["this"], RDF.type, t.uri) in t.lifted
    assert (
        var["this"],
        slot_predicate(t.slug, "invoice"),
        var["invoice"],
    ) in t.lifted
    assert (
        var["this"],
        slot_predicate(t.slug, "value"),
        var["value"],
    ) in t.lifted

    # 1 type triple + 2 slot-predicate triples = 3 total.
    assert len(t.lifted) == 3


def test_passthrough_lowered_uses_skolemized_var_uris():
    t = load_template(FIXTURES / "passthrough_vat.ttl")
    triples = list(t.lowered)
    assert len(triples) == 1
    s, p, o = triples[0]
    # Source-file `var:invoice` (= urn:tpl-var/invoice) is now in the per-
    # template namespace.
    assert s == t.var_ns["invoice"]
    assert p == DOM.hasVatNumber
    assert o == t.var_ns["value"]


def test_sourced_assertion_captures_subject_and_multi_slot():
    t = load_template(FIXTURES / "sourced_assertion.ttl")

    assert t.uri == DG.SourcedAssertion
    assert t.subject == ISO.Description
    assert t.is_instance_form is True
    assert t.slug == "sourced-assertion"

    refs = t.slot("references")
    assert refs is not None
    assert refs.max_count == 0
    assert refs.is_multi is True

    for name in ("doc", "quoteText", "locator"):
        slot = t.slot(name)
        assert slot.min_count == 1
        assert slot.max_count == 1
        assert slot.is_multi is False


def test_sourced_assertion_lowered_replaces_bnodes_with_anon_uris():
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    anon_prefix = str(t.anon_ns)

    composition_subjects = [
        s for s, _, o in t.lowered if o == ISO.CompositionOfIndividual
    ]
    description_subjects = [
        s for s, _, o in t.lowered if o == ISO.Description
    ]
    assert len(composition_subjects) == 1
    assert len(description_subjects) == 1
    assert str(composition_subjects[0]).startswith(anon_prefix)
    assert str(description_subjects[0]).startswith(anon_prefix)
    assert composition_subjects[0] != description_subjects[0]


def test_sourced_assertion_intermediate_var_quote_lives_in_var_ns():
    """var:quote isn't a slot — it's a named intermediate variable. After
    skolemization it should be in the per-template var: namespace."""
    t = load_template(FIXTURES / "sourced_assertion.ttl")
    quote_subjects = [s for s, _, o in t.lowered if o == DG.Quote]
    assert len(quote_subjects) == 1
    assert quote_subjects[0] == t.var_ns["quote"]


def test_sourced_assertion_load_is_idempotent():
    t1 = load_template(FIXTURES / "sourced_assertion.ttl")
    t2 = load_template(FIXTURES / "sourced_assertion.ttl")
    assert sorted(map(lambda tr: (str(tr[0]), str(tr[1]), str(tr[2])), t1.lowered)) == \
           sorted(map(lambda tr: (str(tr[0]), str(tr[1]), str(tr[2])), t2.lowered))


def test_pattern_form_loads_explicit_lifted_and_no_slots():
    t = load_template(FIXTURES / "prov_wgb.ttl")

    assert t.is_instance_form is False
    assert t.slots == []
    assert t.subject == ISO.CompositionOfIndividual
    assert t.slug == "prov-wgb"

    lifted = list(t.lifted)
    assert len(lifted) == 1
    s, p, o = lifted[0]
    assert p == PROV.wasGeneratedBy
    assert s == t.var_ns["entity"]
    assert o == t.var_ns["activity"]

    # Lowered: reified composition tuple (3 triples after bnode rewrite).
    lowered = list(t.lowered)
    assert len(lowered) == 3
    assert any(p == ISO.hasWhole for _, p, _ in lowered)
    assert any(p == ISO.hasPart for _, p, _ in lowered)


def test_missing_lowered_is_an_error(tmp_path):
    bad = tmp_path / "bad.ttl"
    bad.write_text(
        "@prefix tpl: <http://example.org/docgraph/template#> .\n"
        "@prefix ex: <http://example.org/> .\n"
        "ex:T a tpl:Template .\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tpl:lowered"):
        load_template(bad)


def test_no_template_class_is_an_error(tmp_path):
    bad = tmp_path / "bad.ttl"
    bad.write_text(
        "@prefix ex: <http://example.org/> .\n"
        "ex:Foo a ex:Bar .\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no tpl:Template"):
        load_template(bad)


def test_slot_outside_var_namespace_is_rejected(tmp_path):
    bad = tmp_path / "bad.ttl"
    bad.write_text(
        """\
@prefix tpl: <http://example.org/docgraph/template#> .
@prefix ex:  <http://example.org/> .
@prefix var: <urn:tpl-var/> .

ex:T a tpl:Template ;
    tpl:slot ex:notInVarNamespace ;
    tpl:lowered var:lowered .

GRAPH var:lowered { var:x a ex:Y . }
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="var: namespace"):
        load_template(bad)


def test_pattern_form_with_slots_is_rejected(tmp_path):
    bad = tmp_path / "bad.ttl"
    bad.write_text(
        """\
@prefix tpl: <http://example.org/docgraph/template#> .
@prefix ex:  <http://example.org/> .
@prefix var: <urn:tpl-var/> .

ex:T a tpl:Template ;
    tpl:slot var:x ;
    tpl:lifted  var:lifted ;
    tpl:lowered var:lowered .

GRAPH var:lifted  { var:x a ex:A . }
GRAPH var:lowered { var:x a ex:B . }
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="pattern-form"):
        load_template(bad)
