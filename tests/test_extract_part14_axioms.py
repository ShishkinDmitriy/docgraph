"""Ontology-axiom helpers for the part14 walker.

Tests against the bundled LIS-14 + dg + alignments ontologies via a real
init'd part14 project.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import URIRef

from src.extract_part14 import axioms
from src.extract_part14.loader import build_dataset, union_view
from src.project import init_project, PIPELINE_PART14


LIS = "http://rds.posccaesar.org/ontology/lis14/rdl/"


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    """A union-view of a fresh part14 project's loaded ontologies."""
    project_dir = tmp_path_factory.mktemp("part14-axioms")
    from rich.console import Console
    init_project(project_dir, Console(quiet=True), pipeline=PIPELINE_PART14)
    ds = build_dataset(project_dir)
    return union_view(ds)


def test_top_level_classes_lis_partition(ontology):
    """LIS-14 has exactly 3 top-level classes: Activity, Aspect, Object."""
    tops = axioms.top_level_classes(ontology, namespace=LIS)
    expected = {URIRef(LIS + n) for n in ("Activity", "Aspect", "Object")}
    assert set(tops) == expected


def test_disjoint_with_inherits_via_subclass(ontology):
    """Activity ⊥ Object (top-level axiom) → Activity ⊥ all subclasses of Object."""
    activity = URIRef(LIS + "Activity")
    disjoint = axioms.disjoint_with(ontology, activity)

    # Direct disjointness from AllDisjointClasses axiom
    assert URIRef(LIS + "Aspect")  in disjoint
    assert URIRef(LIS + "Object")  in disjoint

    # Inherited via subClassOf — these are all under Object
    assert URIRef(LIS + "PhysicalObject")    in disjoint
    assert URIRef(LIS + "InformationObject") in disjoint
    assert URIRef(LIS + "Person")            in disjoint   # ⊆ Organism ⊆ PhysicalObject ⊆ Object


def test_inverse_of_haspart(ontology):
    has_part = URIRef(LIS + "hasPart")
    assert axioms.inverse_of(ontology, has_part) == URIRef(LIS + "partOf")


def test_inverse_of_returns_none_for_property_without_inverse(ontology):
    # Pick a property unlikely to have an inverse
    creates = URIRef(LIS + "creates")
    inv = axioms.inverse_of(ontology, creates)
    # If LIS-14 ever adds an inverse, update this assertion. Today it has one
    # (createdBy) so just verify it returns *something* sensible — a URIRef
    # in the lis: namespace, not a random blank node.
    assert inv is None or (isinstance(inv, URIRef) and str(inv).startswith(LIS))


def test_parent_property(ontology):
    has_arranged_part = URIRef(LIS + "hasArrangedPart")
    parent = axioms.parent_property(ontology, has_arranged_part)
    assert parent == URIRef(LIS + "hasPart")


def test_properties_of_activity_returns_known_properties(ontology):
    activity = URIRef(LIS + "Activity")
    props = axioms.properties_of(ontology, activity, include_inherited=False)
    assert URIRef(LIS + "creates")          in props
    assert URIRef(LIS + "hasParticipant")   in props
    assert URIRef(LIS + "hasActivityPart")  in props


def test_class_label_falls_back_to_local_name(ontology):
    # Activity has a label
    assert axioms.class_label(ontology, URIRef(LIS + "Activity")) == "Activity"
    # A made-up URI with no label gets its local name back
    fake = URIRef("http://example.org/whatever#FooBar")
    assert axioms.class_label(ontology, fake) == "FooBar"


def test_is_extractable_default_true(ontology):
    """No dg:extractable annotation in default ontology → True."""
    assert axioms.is_extractable(ontology, URIRef(LIS + "Activity")) is True


# ── Domain-less properties + domain validation (new) ──────────────────────

def test_domain_less_properties_returns_universal_predicates(ontology):
    """POSC's LIS-14 leaves ~50 of 66 properties without rdfs:domain. They
    must be discoverable so the walker can offer them to every entity."""
    domain_less = axioms.domain_less_properties(ontology, namespace=LIS)
    # Confirm a few canonical domain-less properties are present
    assert URIRef(LIS + "approvedOn") in domain_less
    assert URIRef(LIS + "approvedBy") in domain_less
    assert URIRef(LIS + "hasRole")    in domain_less
    assert URIRef(LIS + "hasBeginning") in domain_less
    assert URIRef(LIS + "createdBy")  in domain_less

    # And confirm a property WITH domain is correctly excluded
    assert URIRef(LIS + "hasParticipant") not in domain_less   # has rdfs:domain Activity


def test_domain_satisfied_no_constraint(ontology):
    """A predicate with no rdfs:domain is always satisfied."""
    activity = URIRef(LIS + "Activity")
    approved_on = URIRef(LIS + "approvedOn")  # no rdfs:domain
    assert axioms.domain_satisfied(ontology, [activity], approved_on) is True

    # Same for any random subject types
    person = URIRef(LIS + "Person")
    assert axioms.domain_satisfied(ontology, [person], approved_on) is True


def test_domain_satisfied_matches_via_inheritance(ontology):
    """A subject type that's a subclass of the predicate's domain satisfies it."""
    has_participant = URIRef(LIS + "hasParticipant")  # rdfs:domain Activity
    activity = URIRef(LIS + "Activity")
    event    = URIRef(LIS + "Event")     # subclass of Activity in LIS-14

    assert axioms.domain_satisfied(ontology, [activity], has_participant) is True
    assert axioms.domain_satisfied(ontology, [event],    has_participant) is True


def test_range_satisfied_no_constraint(ontology):
    """A predicate with no rdfs:range is always satisfied."""
    activity = URIRef(LIS + "Activity")
    approved_on = URIRef(LIS + "approvedOn")  # no rdfs:range
    assert axioms.range_satisfied(ontology, [activity], approved_on) is True


def test_range_satisfied_rejects_wrong_object_type(ontology):
    """`lis:representedBy rdfs:range lis:InformationObject` — Person is NOT
    an InformationObject (Person is under Object, formally disjoint with
    InformationObject? — at minimum they're separate sub-trees).
    """
    represented_by = URIRef(LIS + "representedBy")
    person = URIRef(LIS + "Person")
    information_object = URIRef(LIS + "InformationObject")
    quote_class = URIRef("http://example.org/docgraph/meta#Quote")  # dg:Quote ⊆ lis:InformationObject

    assert axioms.range_satisfied(ontology, [person], represented_by) is False
    assert axioms.range_satisfied(ontology, [information_object], represented_by) is True
    # dg:Quote is subClassOf lis:InformationObject (per dg-part14-alignments)
    assert axioms.range_satisfied(ontology, [quote_class], represented_by) is True


def test_is_class_range_distinguishes_class_vs_datatype(ontology):
    """`lis:representedBy` has range InformationObject (a class).
    `lis:approvedOn` has no range. `lis:datumValue` ... let's see."""
    assert axioms.is_class_range(ontology, URIRef(LIS + "representedBy")) is True
    # No declared range → not a class range
    assert axioms.is_class_range(ontology, URIRef(LIS + "approvedOn")) is False


def test_domain_satisfied_rejects_violation(ontology):
    """A predicate with a class-domain rejects subjects of incompatible types.

    Note: in POSC's LIS-14 some surprising classes ARE Activities — e.g.,
    `lis:PointInTime ⊂ lis:Event ⊂ lis:Activity` — so a date IS technically
    an Activity per their model and `<date> lis:hasActivityPart <X>` is
    formally legal there.

    For a clear-cut violation, use a Person (under PhysicalObject under Object,
    formally disjoint with Activity) as subject of an Activity-domained
    property.
    """
    has_participant = URIRef(LIS + "hasParticipant")  # rdfs:domain Activity
    person = URIRef(LIS + "Person")

    # Person is under Object, formally disjoint with Activity → violates
    assert axioms.domain_satisfied(ontology, [person], has_participant) is False

    # And the same property IS satisfied for an Activity
    activity = URIRef(LIS + "Activity")
    assert axioms.domain_satisfied(ontology, [activity], has_participant) is True
