"""Convert prompt #3 (Individuals) JSON output to Part 2 Turtle.

Each individual gets the strict-Part-2 pair:
- typed as one of {WholeLifeIndividual, PhysicalObject,
  FunctionalPhysicalObject, SpatialLocation, Stream, ActualIndividual}
- additionally typed by an ad-hoc subclass of the broad ClassOf*
  (one shared subclass per kind, e.g. ext:cls/person  a ClassOfPerson).

The class membership is asserted by plain ``rdf:type`` — no reified
``Classification`` node, since the classification has no metadata of
its own. Reification is reserved for cases where the relationship
itself carries information (third-party assertions, dated
classifications, …).

Aliases become ``skos:altLabel`` triples on the individual itself.

A separate ``locations_of`` list lets the LLM connect already-extracted
individuals to spatial-location individuals (so addresses don't end up
as orphans).

JSON shape::

    {"individuals": [
        {"id":..., "label":..., "kind": "person"|"organization"|...,
         "aliases":[...], "summary":..., "evidence":..., "note":...},
        ...
     ],
     "locations_of": [
        {"individual":"<id>", "location":"<id>"}, ...
     ]}
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, RDF, RDFS, URIRef

from src.classify_part2 import reify
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

# kind → (individual class | None, default ClassOf* metaclass, default kind label).
# None means "no Part 2 kind class beyond the modal axis" — used for `other`,
# the catch-all when the LLM can't decide what category the thing is.
_KIND_MAP: dict[str, tuple[URIRef | None, URIRef, str]] = {
    "person":            (ISO15926.WholeLifeIndividual,    ISO15926.ClassOfPerson,                 "Person"),
    "organization":      (ISO15926.WholeLifeIndividual,    ISO15926.ClassOfOrganization,           "Organization"),
    "physical_object":   (ISO15926.PhysicalObject,         ISO15926.ClassOfInanimatePhysicalObject, "PhysicalObject"),
    "functional_object": (ISO15926.FunctionalPhysicalObject, ISO15926.ClassOfFunctionalObject,    "FunctionalObject"),
    "location":          (ISO15926.SpatialLocation,        ISO15926.ClassOfClassOfIndividual,      "SpatialLocation"),
    "stream":            (ISO15926.Stream,                 ISO15926.ClassOfClassOfIndividual,      "Stream"),
    "other":             (None,                            ISO15926.ClassOfClassOfIndividual,      "Individual"),
}


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    g.bind("skos", SKOS)
    for entry in data.get("individuals") or []:
        _emit_individual(g, entry, ctx)
    for link in data.get("locations_of") or []:
        _emit_location_link(g, link, ctx)
    return g


def _emit_individual(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    iid = entry.get("id")
    if not iid:
        return

    kind = entry.get("kind") or "other"
    individual_cls, metaclass, default_label = _KIND_MAP.get(kind, _KIND_MAP["other"])

    # Three orthogonal Part 2 axes stacked on every individual:
    #   - modal       (§5.2.6.1 ActualIndividual / §5.2.6.11 PossibleIndividual)
    #   - perspective (§5.2.6.15 WholeLifeIndividual — P03 has no time-slice
    #                   semantics so every extracted individual is whole-life)
    #   - kind        (§5.2.6.* concrete subclass from _KIND_MAP, may be None)
    modal_cls = (
        ISO15926.PossibleIndividual
        if entry.get("existence") == "possible"
        else ISO15926.ActualIndividual
    )
    type_set: set[URIRef] = {modal_cls, ISO15926.WholeLifeIndividual}
    if individual_cls is not None:
        type_set.add(individual_cls)

    label = entry.get("label") or iid
    uri = mint_ext(ctx.ext_ns, kind="ind", ident=iid)

    for cls in type_set:
        g.add((uri, RDF.type, cls))
    g.add((uri, RDFS.label, Literal(label)))

    if (summary := entry.get("summary")):
        g.add((uri, RDFS.comment, Literal(summary)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))
    if (note := entry.get("note")):
        g.add((uri, RDFS.comment, Literal(note)))

    # Mint (or reuse) the broad ClassOf* for this kind. Plain rdf:type
    # is sufficient — no reified Classification needed.
    kind_class = reify.mint_class_of(
        g,
        ext_ns=ctx.ext_ns,
        label=default_label,
        metaclass=metaclass,
        seen=ctx.classes_minted,
    )
    g.add((uri, RDF.type, kind_class))

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


def _emit_location_link(g: Graph, link: dict, ctx: ConversionContext) -> None:
    """Connect an individual to a spatial-location individual.

    Uses the docgraph shortcut ``dg:locatedAt`` rather than reifying a
    ``ContainmentOfIndividual`` (Part 2 §5.2.21) — addresses are common
    enough that reification per address would be noisy.
    """
    ind_id = link.get("individual")
    loc_id = link.get("location")
    if not (ind_id and loc_id):
        return
    ind = ctx.get(ind_id)
    loc = ctx.get(loc_id)
    if ind is None or loc is None:
        return
    g.add((ind.uri, DG.locatedAt, loc.uri))
