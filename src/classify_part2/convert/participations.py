"""Convert prompt #7 (Participations).

The `intent` axis dispatches to one of three Part 2 relationship classes:

- ``intent="actual"`` (default) → ``iso15926:Participation`` — the
  participant actually participated in the activity (Part 2 §5.2.9.7).
  IS-A ``CompositionOfIndividual`` with whole=activity, part=participant.

- ``intent="intended"`` → ``iso15926:IntendedRoleAndDomain`` — the
  participant is intended/expected to play the role (Part 2 §5.2.24.3).
  Linked via ``hasPlayer`` (the individual) and ``hasPlayed`` (the role
  concept). No specific activity instance — the role concept already
  carries the domain (kind of activity) from prompt #6.

- ``intent="possible"`` → ``iso15926:PossibleRoleAndDomain`` — the
  participant could possibly play the role (Part 2 §5.2.24.4). Same
  shape as intended.

For ``intent="actual"``, when a role is set, the role concept is attached
via the docgraph shortcut ``dg:hasRole`` rather than reifying an
additional IntendedRoleAndDomain — same trade-off as before.
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
        ind_id = entry.get("participant")
        if not (pid and ind_id):
            continue

        ind = ctx.get(ind_id)
        if ind is None:
            continue

        intent = entry.get("intent") or "actual"
        if intent in ("intended", "possible"):
            _emit_role_and_domain(g, entry, ctx, pid, ind, intent)
        else:
            _emit_participation(g, entry, ctx, pid, ind)

    return g


def _emit_participation(g, entry, ctx, pid, ind) -> None:
    """intent='actual' — Part 2 §5.2.9.7 Participation (CompositionOfIndividual)."""
    act_id = entry.get("activity")
    if not act_id:
        return
    act = ctx.get(act_id)
    if act is None:
        return

    uri = mint_ext(ctx.ext_ns, kind="part", ident=pid)
    g.add((uri, RDF.type, ISO15926.Participation))
    g.add((uri, P.COMPOSITION_WHOLE, act.uri))
    g.add((uri, P.COMPOSITION_PART,  ind.uri))
    if (desc := entry.get("description")):
        g.add((uri, RDFS.comment, Literal(desc)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    if (role_id := entry.get("role")):
        role_ref = ctx.get(role_id)
        if role_ref and role_ref.kind == "role":
            g.add((uri, DG.hasRole, role_ref.uri))

    ctx.register(EntityRef(id=pid, kind="participation", uri=uri,
                           label=f"{ind.label} in {act.label}"))


def _emit_role_and_domain(g, entry, ctx, pid, ind, intent: str) -> None:
    """intent='intended' or 'possible' — Part 2 §5.2.24.3 / §5.2.24.4.

    Skipped if no role is given — these relationships have no meaning
    without a played role concept.
    """
    role_id = entry.get("role")
    if not role_id:
        return
    role_ref = ctx.get(role_id)
    if role_ref is None or role_ref.kind != "role":
        return

    cls = ISO15926.IntendedRoleAndDomain if intent == "intended" else ISO15926.PossibleRoleAndDomain
    uri_kind = "irad" if intent == "intended" else "prad"
    uri = mint_ext(ctx.ext_ns, kind=uri_kind, ident=pid)
    g.add((uri, RDF.type, cls))
    g.add((uri, P.ROLE_PLAYER, ind.uri))
    g.add((uri, P.ROLE_PLAYED, role_ref.uri))
    if (desc := entry.get("description")):
        g.add((uri, RDFS.comment, Literal(desc)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    ctx.register(EntityRef(id=pid, kind="participation", uri=uri,
                           label=f"{ind.label} {intent} as {role_ref.label}"))
