"""Convert prompts #4 (Classes of activity) and #5 (Classes of individual).

Both prompts emit class-level taxonomy: a list of class entries where
each can have a parent (within the same prompt) and a list of already-
extracted instances. The Turtle output is symmetric across the two
prompts; only the metaclass and entity-kind label differ.
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS, URIRef

from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

# Prompt #4 — kind label → metaclass
_ACTIVITY_KINDS = {
    "ClassOfActivity":     ISO15926.ClassOfActivity,
    "ClassOfEvent":        ISO15926.ClassOfEvent,
    "ClassOfPeriodInTime": ISO15926.ClassOfPeriodInTime,
    "ClassOfPointInTime":  ISO15926.ClassOfPointInTime,
}

# Prompt #5 — kind label → metaclass
_INDIVIDUAL_KINDS = {
    "person":              ISO15926.ClassOfPerson,
    "organization":        ISO15926.ClassOfOrganization,
    "physical_object":     ISO15926.ClassOfInanimatePhysicalObject,
    "functional_object":   ISO15926.ClassOfFunctionalObject,
    "information_object":  ISO15926.ClassOfInformationObject,
    "material":            ISO15926.ClassOfCompositeMaterial,
    "feature":             ISO15926.ClassOfFeature,
    "organism":            ISO15926.ClassOfOrganism,
    "arranged_individual": ISO15926.ClassOfArrangedIndividual,
    "other":               ISO15926.ClassOfClassOfIndividual,
}


def convert_activities(data: dict, ctx: ConversionContext) -> Graph:
    """Prompt #4 — `classes_of_activity`."""
    g = Graph()
    entries = data.get("classes_of_activity") or []
    # Two passes: emit nodes, then resolve `parent` references which may
    # point at sibling entries we hadn't yet minted.
    minted: dict[str, URIRef] = {}
    for entry in entries:
        cid = entry.get("id")
        if not cid:
            continue
        metaclass = _ACTIVITY_KINDS.get(entry.get("iso_class") or "ClassOfActivity",
                                        ISO15926.ClassOfActivity)
        uri = _emit_class(g, ctx, entry, metaclass=metaclass, kind_tag="class_of_activity")
        minted[cid] = uri
    for entry in entries:
        _link_parent_and_instances(g, ctx, entry, minted, instance_kind="activity")
    return g


def convert_individuals(data: dict, ctx: ConversionContext) -> Graph:
    """Prompt #5 — `classes_of_individual`."""
    g = Graph()
    entries = data.get("classes_of_individual") or []
    minted: dict[str, URIRef] = {}
    for entry in entries:
        cid = entry.get("id")
        if not cid:
            continue
        metaclass = _INDIVIDUAL_KINDS.get(entry.get("kind") or "other",
                                          ISO15926.ClassOfClassOfIndividual)
        uri = _emit_class(g, ctx, entry, metaclass=metaclass, kind_tag="class_of_individual")
        minted[cid] = uri
        if (entry.get("kind") or "other") == "other":
            g.add((uri, DG.status, DG.Unresolved))
    for entry in entries:
        _link_parent_and_instances(g, ctx, entry, minted, instance_kind="individual")
    return g


def _emit_class(
    g: Graph,
    ctx: ConversionContext,
    entry: dict,
    *,
    metaclass: URIRef,
    kind_tag: str,
) -> URIRef:
    cid = entry["id"]
    label = entry.get("label") or cid
    uri = mint_ext(ctx.ext_ns, kind="cls", ident=cid)
    g.add((uri, RDF.type, metaclass))
    g.add((uri, RDFS.label, Literal(label)))
    if (definition := entry.get("definition")):
        g.add((uri, RDFS.comment, Literal(definition)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))
    if (note := entry.get("note")):
        g.add((uri, RDFS.comment, Literal(note)))
    ctx.register(EntityRef(
        id=cid, kind=kind_tag, uri=uri, label=label,
        subkind=entry.get("kind") or entry.get("iso_class") or "",
    ))
    # Memoise so other converters can reuse this class via ctx.classes_minted.
    ctx.classes_minted[cid] = uri
    return uri


def _link_parent_and_instances(
    g: Graph,
    ctx: ConversionContext,
    entry: dict,
    minted: dict[str, URIRef],
    *,
    instance_kind: str,
) -> None:
    cid = entry.get("id")
    if not cid or cid not in minted:
        return
    cls_uri = minted[cid]

    # Parent → rdfs:subClassOf inside this prompt's output.
    if (parent := entry.get("parent")) and parent in minted:
        g.add((cls_uri, RDFS.subClassOf, minted[parent]))

    # Instances → typed by this class via plain rdf:type. Reified
    # Classification is reserved for third-party assertions about
    # classifications.
    for inst_id in entry.get("instances") or []:
        ref = ctx.get(inst_id)
        if ref is None or ref.kind != instance_kind:
            continue
        g.add((ref.uri, RDF.type, cls_uri))
