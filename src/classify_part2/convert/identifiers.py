"""Convert prompt #12 (Identifiers & descriptions)."""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, RDF

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

_KIND_TO_CLASS = {
    "identifier":      ISO15926.Identification,
    "name":            ISO15926.Identification,
    "alias":           ISO15926.Identification,
    "description":     ISO15926.Description,
    "definition":      ISO15926.Definition,
    "cross_reference": ISO15926.RepresentationOfThing,
}


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    g.bind("skos", SKOS)
    for entry in data.get("representations") or []:
        rid = entry.get("id")
        rep_kind = entry.get("representation_kind")
        target_id = entry.get("represents")
        value = entry.get("value")
        if not (rid and rep_kind and target_id and value is not None):
            continue
        target = ctx.get(target_id)
        if target is None:
            continue

        cls = _KIND_TO_CLASS.get(rep_kind, ISO15926.RepresentationOfThing)
        uri = mint_ext(ctx.ext_ns, kind="rep", ident=rid)
        g.add((uri, RDF.type, cls))
        g.add((uri, P.REPR_REPRESENTED, target.uri))

        if rep_kind == "cross_reference":
            g.add((uri, DG.externalRef, Literal(str(value))))
        else:
            g.add((uri, DG.value, Literal(str(value))))

        if rep_kind == "name":
            g.add((uri, DG.nameKind, Literal("name")))
        elif rep_kind == "alias":
            g.add((uri, DG.nameKind, Literal("alias")))
            # Convenience shortcut on the target itself.
            g.add((target.uri, SKOS.altLabel, Literal(str(value))))

        if (system := entry.get("system")):
            g.add((uri, DG.system, Literal(system)))
        if (desc := entry.get("description")):
            g.add((uri, DG.summary, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        ctx.register(EntityRef(id=rid, kind="representation", uri=uri,
                               label=f"{rep_kind}: {value}"))
    return g
