"""Convert prompt #7 (Participations).

A Participation IS-A CompositionOfIndividual, so we use the inherited
``hasWhole`` (= activity) and ``hasPart`` (= participant) properties.
When a role is set, an additional ``IntendedRoleAndDomain`` is reified
linking the participant + role + activity.
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    for entry in data.get("participations") or []:
        pid = entry.get("id")
        act_id = entry.get("activity")
        ind_id = entry.get("participant")
        if not (pid and act_id and ind_id):
            continue

        act = ctx.get(act_id)
        ind = ctx.get(ind_id)
        if act is None or ind is None:
            continue

        uri = mint_ext(ctx.ext_ns, kind="part", ident=pid)
        g.add((uri, RDF.type, ISO15926.Participation))
        g.add((uri, P.COMPOSITION_WHOLE, act.uri))
        g.add((uri, P.COMPOSITION_PART,  ind.uri))
        if (desc := entry.get("description")):
            g.add((uri, DG.summary, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        # Optional role: reify an IntendedRoleAndDomain linking the player
        # (the participant individual's class is unknown here, so we link
        # at instance level) to the activity domain.
        role_id = entry.get("role")
        if role_id:
            role_ref = ctx.get(role_id)
            if role_ref and role_ref.kind == "role":
                irad = mint_ext(ctx.ext_ns, kind="role-link", ident=pid)
                g.add((irad, RDF.type, ISO15926.IntendedRoleAndDomain))
                g.add((irad, P.ROLE_PLAYER, ind.uri))
                g.add((irad, P.ROLE_DOMAIN, act.uri))
                g.add((irad, RDF.type, role_ref.uri))   # classified by the role concept
                g.add((uri, DG.hasRoleLink, irad))

        ctx.register(EntityRef(id=pid, kind="participation", uri=uri,
                               label=f"{ind.label} in {act.label}"))
    return g
