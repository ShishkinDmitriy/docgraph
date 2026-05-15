"""Tests for src/extract_part14/ext_ontology.py — extension class
proposal/merge plumbing for the mega-walker.

Three-tier storage model: per-doc graphs (proposed) → project-wide
ext.ttl (promoted) → external RDL. This module covers the "proposed"
tier mechanics: build class definition triples for inclusion in a per-
doc graph, read them back from any graph (union view), merge proposals
with reuse-before-mint policy.
"""

from __future__ import annotations

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from src.extract_part14.ext_ontology import (
    BLACKLISTED_ANCHORS,
    DG,
    EXT,
    LIS,
    ExtClass,
    class_definitions_graph,
    extract_classes_from_graph,
    is_allowed_anchor,
    merge_proposals,
    normalize_slug,
    to_camel_case,
)


# ── normalize_slug ─────────────────────────────────────────────────────────

def test_slug_strips_non_alphanumeric():
    assert normalize_slug("IBAN") == "IBAN"
    assert normalize_slug("I.B.A.N.") == "IBAN"
    assert normalize_slug("iban_id") == "ibanid"
    assert normalize_slug("Bank Account #") == "BankAccount"


def test_slug_handles_empty_input():
    assert normalize_slug("???") == "Unnamed"
    assert normalize_slug("") == "Unnamed"


# ── to_camel_case (used to normalize label + alt_labels) ──────────────────

def test_camel_case_collapses_spaces():
    assert to_camel_case("Bank Account") == "BankAccount"
    assert to_camel_case("international bank account") == "InternationalBankAccount"


def test_camel_case_preserves_all_caps_acronyms():
    """IBAN, BIC, USD shouldn't get squashed into "Iban" / "Bic" / "Usd"."""
    assert to_camel_case("IBAN") == "IBAN"
    assert to_camel_case("BIC code") == "BICCode"


def test_camel_case_handles_separators():
    assert to_camel_case("billing_document") == "BillingDocument"
    assert to_camel_case("dental-procedure") == "DentalProcedure"
    assert to_camel_case("sub.location.of") == "SubLocationOf"


def test_camel_case_handles_empty_or_garbage():
    assert to_camel_case("") == ""
    assert to_camel_case("   ") == ""


# ── class_definitions_graph ────────────────────────────────────────────────

def test_definitions_graph_emits_owl_class_triple():
    g = class_definitions_graph([
        ExtClass(slug="IBAN", anchor=LIS.InformationObject, label="IBAN"),
    ])
    assert (EXT.IBAN, RDF.type, OWL.Class) in g
    assert (EXT.IBAN, RDFS.subClassOf, LIS.InformationObject) in g


def test_definitions_graph_emits_label_alts_comment_provenance():
    g = class_definitions_graph([
        ExtClass(
            slug="IBAN",
            anchor=LIS.InformationObject,
            label="IBAN",
            alt_labels=["International Bank Account Number", "Bank Account ID"],
            comment="Globally unique account identifier (ISO 13616).",
            provenance="proposed-by-llm",
            first_seen=URIRef("http://example.org/source/inv2025"),
        ),
    ])
    triples = set(g)
    assert (EXT.IBAN, RDFS.label, ...) not in triples  # placeholder check below
    # Now check actual values
    assert any(p == RDFS.label for s, p, o in g.triples((EXT.IBAN, RDFS.label, None)))
    alts = list(g.objects(EXT.IBAN, SKOS.altLabel))
    assert len(alts) == 2
    comment = next(g.objects(EXT.IBAN, RDFS.comment))
    assert "ISO 13616" in str(comment)
    prov = next(g.objects(EXT.IBAN, DG.provenance))
    assert str(prov) == "proposed-by-llm"
    seen = next(g.objects(EXT.IBAN, DG.firstSeenIn))
    assert str(seen) == "http://example.org/source/inv2025"


def test_definitions_graph_omits_empty_optional_fields():
    g = class_definitions_graph([
        ExtClass(slug="X", anchor=LIS.Person, label="X"),
    ])
    assert list(g.objects(EXT.X, RDFS.comment)) == []
    assert list(g.objects(EXT.X, SKOS.altLabel)) == []
    assert list(g.objects(EXT.X, DG.firstSeenIn)) == []
    # Provenance default is set
    assert str(next(g.objects(EXT.X, DG.provenance))) == "proposed-by-llm"


def test_definitions_graph_accepts_dict_or_list():
    """Convenience: caller can pass either a list of ExtClass or the dict
    shape used by merge_proposals."""
    cls = ExtClass(slug="X", anchor=LIS.Person, label="X")
    g_list = class_definitions_graph([cls])
    g_dict = class_definitions_graph({"X": cls})
    assert (EXT.X, RDF.type, OWL.Class) in g_list
    assert (EXT.X, RDF.type, OWL.Class) in g_dict


# ── extract_classes_from_graph (round-trip) ────────────────────────────────

def test_round_trip_definitions_to_graph_and_back():
    original = ExtClass(
        slug="IBAN",
        anchor=LIS.InformationObject,
        label="IBAN",
        alt_labels=["International Bank Account Number"],
        comment="Globally unique account identifier (ISO 13616).",
        provenance="proposed-by-llm",
        first_seen=URIRef("http://example.org/source/inv2025"),
    )
    g = class_definitions_graph([original])

    loaded = extract_classes_from_graph(g)
    assert "IBAN" in loaded
    iban = loaded["IBAN"]
    assert iban.label == "IBAN"
    assert "International Bank Account Number" in iban.alt_labels
    assert iban.anchor == LIS.InformationObject
    assert "ISO 13616" in iban.comment
    assert iban.provenance == "proposed-by-llm"
    assert iban.first_seen == URIRef("http://example.org/source/inv2025")


def test_extract_skips_non_ext_classes():
    """Classes outside the ext: namespace are ignored — this loader is
    only for proposed/promoted classes, not the bundled ontology."""
    g = Graph()
    g.add((LIS.Outsider, RDF.type, OWL.Class))
    g.add((LIS.Outsider, RDFS.subClassOf, LIS.InformationObject))
    g.add((LIS.Outsider, RDFS.label, __import__("rdflib").Literal("Outsider")))
    assert extract_classes_from_graph(g) == {}


def test_extract_merges_duplicate_definitions_across_docs():
    """Two doc graphs each declaring `ext:IBAN` slightly differently
    union into one entry: longest label/comment wins, altLabels merge."""
    from rdflib import Literal

    g = Graph()
    # Doc 1's definition
    g.add((EXT.IBAN, RDF.type, OWL.Class))
    g.add((EXT.IBAN, RDFS.subClassOf, LIS.InformationObject))
    g.add((EXT.IBAN, RDFS.label, Literal("IBAN")))
    g.add((EXT.IBAN, RDFS.comment, Literal("Short.")))
    g.add((EXT.IBAN, SKOS.altLabel, Literal("BankAccountId")))
    # Doc 2's definition (longer label, longer comment, different alts)
    g.add((EXT.IBAN, RDFS.label, Literal("IBAN code (ISO 13616)")))
    g.add((EXT.IBAN, RDFS.comment, Literal("A globally unique account identifier per ISO 13616.")))
    g.add((EXT.IBAN, SKOS.altLabel, Literal("International Bank Account Number")))

    loaded = extract_classes_from_graph(g)
    assert "IBAN" in loaded
    iban = loaded["IBAN"]
    assert iban.label == "IBAN code (ISO 13616)"   # longest wins
    assert "ISO 13616" in iban.comment              # longest wins
    assert "BankAccountId" in iban.alt_labels
    assert "International Bank Account Number" in iban.alt_labels


# ── merge_proposals ────────────────────────────────────────────────────────

def test_merge_appends_new_proposals():
    existing: dict[str, ExtClass] = {}
    proposals = [
        ExtClass(slug="IBAN", anchor=LIS.InformationObject, label="IBAN"),
        ExtClass(slug="BIC",  anchor=LIS.InformationObject, label="BIC"),
    ]
    merged, newly = merge_proposals(existing, proposals)
    assert set(merged.keys()) == {"IBAN", "BIC"}
    assert {n.slug for n in newly} == {"IBAN", "BIC"}


def test_merge_dedupes_by_slug_and_unions_alt_labels():
    """Same slug → existing wins on canonical fields, but alt-labels merge.
    This is the reuse-before-mint policy — the LLM is told "if the slug
    already exists, you're reusing, not redefining."""
    existing = {
        "IBAN": ExtClass(
            slug="IBAN", anchor=LIS.InformationObject, label="IBAN",
            alt_labels=["International Bank Account Number"],
        ),
    }
    proposals = [
        ExtClass(
            slug="IBAN", anchor=LIS.InformationObject, label="IBAN code",
            alt_labels=["Bank Account ID"],
        ),
    ]
    merged, newly = merge_proposals(existing, proposals)
    assert newly == []          # not newly added; reused existing
    assert merged["IBAN"].label == "IBAN"   # existing canonical kept
    # New label became an alt; new alt joined the union.
    assert "Bank Account ID" in merged["IBAN"].alt_labels
    assert "IBAN code"       in merged["IBAN"].alt_labels
    assert "International Bank Account Number" in merged["IBAN"].alt_labels


def test_merge_does_not_duplicate_alt_labels():
    existing = {
        "IBAN": ExtClass(
            slug="IBAN", anchor=LIS.InformationObject, label="IBAN",
            alt_labels=["IBAN code"],
        ),
    }
    proposals = [
        ExtClass(slug="IBAN", anchor=LIS.InformationObject, label="IBAN",
                 alt_labels=["IBAN code"]),
    ]
    merged, _ = merge_proposals(existing, proposals)
    assert merged["IBAN"].alt_labels.count("IBAN code") == 1


# ── Anchor blacklist ───────────────────────────────────────────────────────

def test_blacklist_excludes_only_overly_abstract_roots():
    """Only the over-abstract roots are blocked — everything else in LIS-14
    is fair game as an ext: anchor, given the class actually exists."""
    assert LIS.Object in BLACKLISTED_ANCHORS
    assert LIS.Aspect in BLACKLISTED_ANCHORS


def test_concrete_classes_are_allowed_anchors():
    for cls in (LIS.Person, LIS.Organization, LIS.InformationObject,
                LIS.FunctionalObject, LIS.Activity, LIS.Site,
                LIS.Role, LIS.Function, LIS.Disposition):
        assert is_allowed_anchor(cls), f"{cls} should be a permitted anchor"


def test_overly_abstract_roots_are_not_allowed():
    assert not is_allowed_anchor(LIS.Object)
    assert not is_allowed_anchor(LIS.Aspect)
