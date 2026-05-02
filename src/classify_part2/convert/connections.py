"""Convert prompt #13 (Connections).

Direct or indirect connections between two individuals, optionally with a
medium (the carrier) and an intermediary individual (for indirect). The
medium is also reified as ``IndividualUsedInConnection``. New individuals
declared in the prompt's ``new_individuals`` list are folded into the
context first.
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.convert import individuals as conv_inds
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()

    # Fold any new individuals first.
    new_inds = data.get("new_individuals") or []
    if new_inds:
        g += conv_inds.convert({"individuals": new_inds}, ctx)

    for entry in data.get("connections") or []:
        cid     = entry.get("id")
        from_id = entry.get("from")
        to_id   = entry.get("to")
        if not (cid and from_id and to_id):
            continue
        a = ctx.get(from_id)
        b = ctx.get(to_id)
        if a is None or b is None:
            continue

        kind = entry.get("connection_kind") or "direct"
        cls = ISO15926.IndirectConnection if kind == "indirect" else ISO15926.DirectConnection

        uri = mint_ext(ctx.ext_ns, kind="conn", ident=cid)
        g.add((uri, RDF.type, cls))
        g.add((uri, P.CONNECTION_SIDE1, a.uri))
        g.add((uri, P.CONNECTION_SIDE2, b.uri))

        if (nature := entry.get("nature")):
            g.add((uri, DG.nature, Literal(nature)))
        direction = entry.get("direction") or "unspecified"
        if direction != "unspecified":
            g.add((uri, DG.direction, Literal(direction)))
        if (desc := entry.get("description")):
            g.add((uri, RDFS.comment, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        if (via_id := entry.get("via")):
            via_ref = ctx.get(via_id)
            if via_ref:
                g.add((uri, DG.via, via_ref.uri))

        if (medium_id := entry.get("medium")):
            medium_ref = ctx.get(medium_id)
            if medium_ref:
                used_uri = mint_ext(ctx.ext_ns, kind="used", ident=cid)
                g.add((used_uri, RDF.type, ISO15926.IndividualUsedInConnection))
                g.add((used_uri, P.USED_IN_CONN_USAGE,      medium_ref.uri))
                g.add((used_uri, P.USED_IN_CONN_CONNECTION, uri))

        ctx.register(EntityRef(id=cid, kind="connection", uri=uri,
                               label=f"{a.label} ↔ {b.label}"))
    return g
