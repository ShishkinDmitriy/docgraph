"""Tests for sub-template invocation expansion (load-time inlining).

Verifies:
- A leaf wrapper template (`tpl:StringValue`) loads stand-alone
- An outer template that uses `[ a tpl:StringValue ; tpl:literal var:value ]`
  to invoke the wrapper produces a fully-flat Part 2 lowered body — no
  invocation markers survive
- Variables substitute correctly: inner `var:this` → invocation node;
  inner `var:literal` slot → outer's bound `var:value`; inner anon URIs are
  re-minted into the outer's anon namespace
- Round-trip expand → recognize works on the wrapped form
- Loading the outer without a registry leaves the invocation triples in place
  (no auto-discovery of leaf templates; explicit registry only)
"""

from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from src.templates.expand import expand
from src.templates.loader import load_template
from src.templates.recognize import recognize

FIXTURES = Path(__file__).parent / "fixtures" / "templates"

DOM = Namespace("http://example.org/docgraph/financial#")
ISO = Namespace("http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#")
TPL = Namespace("http://example.org/docgraph/template#")
EX = Namespace("http://example.org/x/")


def _load_with_string_value() -> tuple:
    """Load tpl:StringValue, then the outer template with that registry."""
    sv = load_template(FIXTURES / "string_value.ttl")
    outer = load_template(
        FIXTURES / "invoice_with_typed_vat.ttl",
        registry={sv.uri: sv},
    )
    return sv, outer


def test_string_value_loads_standalone():
    """The leaf wrapper has one slot and a 2-triple lowered body."""
    sv = load_template(FIXTURES / "string_value.ttl")
    assert sv.uri == TPL.StringValue
    assert sv.slug == "string-value"
    assert {s.name for s in sv.slots} == {"literal"}
    assert sv.subject == ISO.ClassOfInformationRepresentation

    triples = list(sv.lowered)
    assert len(triples) == 2
    # var:this a iso:ClassOfInformationRepresentation
    assert (sv.var_ns["this"], RDF.type, ISO.ClassOfInformationRepresentation) in sv.lowered
    # var:this iso:hasLiteral var:literal
    assert (sv.var_ns["this"], ISO.hasLiteral, sv.var_ns["literal"]) in sv.lowered


def test_outer_template_inlines_string_value_invocation():
    """Outer template's lowered body has the invocation expanded into Part 2
    triples; no invocation markers survive."""
    _, outer = _load_with_string_value()
    triples = list(outer.lowered)
    assert len(triples) == 3, f"expected 3 triples, got {len(triples)}: {triples}"

    # No `?x rdf:type tpl:StringValue` anywhere (invocation marker removed).
    assert not any(
        p == RDF.type and o == TPL.StringValue for s, p, o in triples
    ), "rdf:type tpl:StringValue invocation marker should have been dropped"
    # No `tpl:literal` binding triple either (only `iso:hasLiteral` should
    # remain — that's the inner template's own predicate).
    for _, p, _ in triples:
        if _localname(p) == "literal":
            assert p == ISO.hasLiteral, (
                f"expected only iso:hasLiteral with localname 'literal', "
                f"got {p!r}"
            )

    # The invocation bnode became an anon URI in the OUTER template's namespace.
    anon_subjects = [
        s for s, _, _ in triples if str(s).startswith(str(outer.anon_ns))
    ]
    assert anon_subjects, "expected an outer-anon URI as the invocation result"
    cluster_anchor = anon_subjects[0]

    # The cluster anchor carries the inner's type and hasLiteral pointing at
    # the OUTER's `var:value` slot.
    assert (cluster_anchor, RDF.type, ISO.ClassOfInformationRepresentation) in outer.lowered
    assert (cluster_anchor, ISO.hasLiteral, outer.var_ns["value"]) in outer.lowered

    # The outer's original triple still references the same anchor.
    assert (outer.var_ns["invoice"], DOM.hasVatNumber, cluster_anchor) in outer.lowered


def test_outer_anchor_lives_in_outer_anon_namespace_not_inner():
    """Sanity: no inner-template URIs leaked into the outer's lowered body."""
    sv, outer = _load_with_string_value()
    inner_var = str(sv.var_ns)
    inner_anon = str(sv.anon_ns)
    for s, p, o in outer.lowered:
        for term in (s, p, o):
            if not isinstance(term, URIRef):
                continue
            t = str(term)
            assert not t.startswith(inner_var), (
                f"inner var URI {term!r} leaked into outer.lowered"
            )
            assert not t.startswith(inner_anon), (
                f"inner anon URI {term!r} leaked into outer.lowered"
            )


def test_outer_template_loads_without_registry_leaves_invocation_intact():
    """No registry → no invocation expansion; the bnode and marker triples
    survive into the lowered body. (Behaviour: the loader is explicit, not
    auto-discovering.)"""
    outer = load_template(FIXTURES / "invoice_with_typed_vat.ttl")
    has_marker = any(
        p == RDF.type and o == TPL.StringValue for s, p, o in outer.lowered
    )
    assert has_marker, "expected the rdf:type tpl:StringValue marker to survive"


def test_expand_outer_produces_part2_wrapper_in_storage():
    """Expansion of the outer template yields the Part 2 reified cluster in
    storage, not just a bare `dom:hasVatNumber` triple."""
    _, outer = _load_with_string_value()

    g = expand(
        outer,
        {"invoice": EX["invoice-001"], "value": "DE123456789"},
    )

    # Three triples: outer link + cluster type + literal-of-cluster.
    triples = list(g)
    assert len(triples) == 3

    # Locate the cluster anchor — it's the object of `dom:hasVatNumber`.
    anchor = list(g.objects(EX["invoice-001"], DOM.hasVatNumber))
    assert len(anchor) == 1
    cluster = anchor[0]

    assert (cluster, RDF.type, ISO.ClassOfInformationRepresentation) in g
    assert (cluster, ISO.hasLiteral, Literal("DE123456789", datatype=XSD.string)) in g


def test_recognize_round_trip_through_invoked_wrapper():
    """expand → recognize round-trips: even with the Part 2 wrapper inserted
    by invocation, the recognizer recovers the original slot bindings."""
    _, outer = _load_with_string_value()

    bindings = {"invoice": EX["invoice-001"], "value": "DE123456789"}
    matches = recognize(outer, expand(outer, bindings))

    assert len(matches) == 1
    m = matches[0]
    assert m["invoice"] == EX["invoice-001"]
    assert m["value"] == Literal("DE123456789", datatype=XSD.string)


def _localname(uri):
    s = str(uri)
    if "#" in s:
        return s.rsplit("#", 1)[1]
    return s.rsplit("/", 1)[1]
