"""Static map of Part 2 OWL object properties used by the converter.

URIs were verified against ``docs/ISO-15926-2_2003.rdf`` on 2026-04-30.
Several Part 2 classes (``Activity``, ``Property``, ``Identification``,
the connection subclasses) inherit their properties from a parent —
this map captures the inherited names directly so the converter
doesn't have to walk the OWL subclass chain at runtime.
"""

from rdflib import URIRef

from src.classify_part2.ns import ISO15926


def _p(local: str) -> URIRef:
    return URIRef(ISO15926[local])


# Approval (Part 2 §5.2.23). Note: Part 2 says hasApproved's range is a
# Relationship — we accept any URI in v1 (semantic looseness flagged in
# docs/classify_design.md).
APPROVAL_APPROVER  = _p("hasApprover")
APPROVAL_APPROVED  = _p("hasApproved")

# Classification (Part 2 §5.2.2 / §4.8.1).
CLASSIFICATION_CLASSIFIER = _p("hasClassifier")
CLASSIFICATION_CLASSIFIED = _p("hasClassified")

# Specialization (Part 2 §4.8.2). Used when one class is a subtype of
# another and we want the relation reified rather than just rdfs:subClassOf.
SPECIALIZATION_SUBCLASS   = _p("hasSubclass")
SPECIALIZATION_SUPERCLASS = _p("hasSuperclass")

# CauseOfEvent (Part 2 §5.2.9).
CAUSE_CAUSER = _p("hasCauser")
CAUSE_CAUSED = _p("hasCaused")

# CompositionOfIndividual (Part 2 §4.7.1) and its subclasses.
# Participation IS-A CompositionOfIndividual, so participations also use
# hasWhole / hasPart with activity = whole, participant = part.
COMPOSITION_WHOLE = _p("hasWhole")
COMPOSITION_PART  = _p("hasPart")

# TemporalSequence (Part 2 §5.2.22).
TEMPORAL_PREDECESSOR = _p("hasPredecessor")
TEMPORAL_SUCCESSOR   = _p("hasSuccessor")

# IndividualUsedInConnection (Part 2 §5.2.21).
USED_IN_CONN_USAGE      = _p("hasUsage")       # the connecting individual
USED_IN_CONN_CONNECTION = _p("hasConnection")  # the connection itself

# LifecycleStage (Part 2 §5.2.23). Note: Part 2's semantics is "an
# interested party has an interest in this thing at this stage". We use
# hasInterest = the subject (thing in this stage), hasInterested = the
# interested party (often unknown — left out when null).
LIFECYCLE_INTEREST    = _p("hasInterest")
LIFECYCLE_INTERESTED  = _p("hasInterested")

# Roles (Part 2 §5.2.13 / §5.2.24).
ROLE_DOMAIN = _p("hasDomain")
ROLE_PLAYER = _p("hasPlayer")

# RepresentationOfThing and subclasses Identification, Definition,
# Description (Part 2 §5.2.16). hasSign points at the sign (a
# possible_individual carrying the actual identifier text); hasRepresented
# points at the thing the sign refers to.
REPR_REPRESENTED = _p("hasRepresented")
REPR_SIGN        = _p("hasSign")

# Property (Part 2 §5.2.26). The bearer is the possessor.
PROPERTY_POSSESSOR = _p("hasPossessor")
