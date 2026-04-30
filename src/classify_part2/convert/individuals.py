"""Convert prompt #3 (Individuals) JSON output to Part 2 Turtle.

Each individual gets the strict-Part-2 pair:
- typed as one of {WholeLifeIndividual, PhysicalObject,
  FunctionalPhysicalObject, SpatialLocation, Stream, ActualIndividual}
- additionally typed by an ad-hoc subclass of the broad ClassOf*
  (one shared subclass per kind, e.g. ext:cls/person  a ClassOfPerson).

A reified ``Classification`` ties the individual to the class.
Aliases get ``skos:altLabel`` triples.

JSON shape::

    {"individuals": [
        {"id":..., "label":..., "kind": "person"|"organization"|...,
         "aliases":[...], "summary":..., "evidence":..., "note":...},
        ...
    ]}
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef

from src.classify_part2 import reify
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

# kind → (individual class, default ClassOf* metaclass, default kind label)
_KIND_MAP: dict[str, tuple[URIRef, URIRef, str]] = {
    "person":            (ISO15926.WholeLifeIndividual,    ISO15926.ClassOfPerson,                 "Person"),
    "organization":      (ISO15926.WholeLifeIndividual,    ISO15926.ClassOfOrganization,           "Organization"),
    "physical_object":   (ISO15926.PhysicalObject,         ISO15926.ClassOfInanimatePhysicalObject, "PhysicalObject"),
    "functional_object": (ISO15926.FunctionalPhysicalObject, ISO15926.ClassOfFunctionalObject,    "FunctionalObject"),
    "location":          (ISO15926.SpatialLocation,        ISO15926.ClassOfClassOfIndividual,      "SpatialLocation"),
    "stream":            (ISO15926.Stream,                 ISO15926.ClassOfClassOfIndividual,      "Stream"),
    "other":             (ISO15926.ActualIndividual,       ISO15926.ClassOfClassOfIndividual,      "Individual"),
}


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    g.bind("skos", SKOS)
    for entry in data.get("individuals") or []:
        _emit_individual(g, entry, ctx)
    return g


def _emit_individual(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    iid = entry.get("id")
    if not iid:
        return

    kind = entry.get("kind") or "other"
    individual_cls, metaclass, default_label = _KIND_MAP.get(kind, _KIND_MAP["other"])

    label = entry.get("label") or iid
    uri = mint_ext(ctx.ext_ns, kind="ind", ident=iid)

    # Strict Part 2: individual is typed by its broad individual class plus
    # an ad-hoc ClassOf* subclass that captures the kind label.
    g.add((uri, RDF.type, individual_cls))
    g.add((uri, RDFS.label, Literal(label)))

    if (summary := entry.get("summary")):
        g.add((uri, DG.summary, Literal(summary)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))
    if (note := entry.get("note")):
        g.add((uri, DG.note, Literal(note)))

    # Mint (or reuse) the broad ClassOf* for this kind.
    kind_class = reify.mint_class_of(
        g,
        ext_ns=ctx.ext_ns,
        label=default_label,
        metaclass=metaclass,
        seen=ctx.classes_minted,
    )

    # Reified Classification: the individual belongs to the kind-class.
    g.add((uri, RDF.type, kind_class))
    reify.classification(
        g,
        ext_ns=ctx.ext_ns,
        classifier=kind_class,
        classified=uri,
        suffix=iid,
    )

    if kind == "other":
        g.add((uri, DG.status, DG.Unresolved))

    aliases = entry.get("aliases") or []
    for alt in aliases:
        if alt:
            g.add((uri, SKOS.altLabel, Literal(alt)))

    ctx.register(EntityRef(
        id=iid, kind="individual", uri=uri, label=label,
        subkind=kind,
    ))
