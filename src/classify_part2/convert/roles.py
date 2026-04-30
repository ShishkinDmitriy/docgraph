"""Convert prompt #6 (Roles).

Roles are emitted as ``iso15926:ClassOfPossibleRoleAndDomain`` instances
(per the design — Part 2 has no standalone Role class). Optional
``domain`` and ``player`` link to already-extracted classes.
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    for entry in data.get("roles") or []:
        rid = entry.get("id")
        if not rid:
            continue
        label = entry.get("label") or rid
        uri = mint_ext(ctx.ext_ns, kind="role", ident=rid)
        g.add((uri, RDF.type, ISO15926.ClassOfPossibleRoleAndDomain))
        g.add((uri, RDFS.label, Literal(label)))
        if (desc := entry.get("description")):
            g.add((uri, RDFS.comment, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        if (domain_id := entry.get("domain")):
            ref = ctx.get(domain_id)
            if ref and ref.kind == "class_of_activity":
                g.add((uri, P.ROLE_DOMAIN, ref.uri))
        if (player_id := entry.get("player")):
            ref = ctx.get(player_id)
            if ref and ref.kind == "class_of_individual":
                g.add((uri, P.ROLE_PLAYER, ref.uri))

        ctx.register(EntityRef(id=rid, kind="role", uri=uri, label=label))
    return g
