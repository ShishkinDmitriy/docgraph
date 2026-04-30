"""Convert prompt #9 (Temporal relationships).

before / after / follows / during / overlaps / concurrent →
``iso15926:TemporalSequence`` (with optional `dg:overlap` qualifier for
the overlap-y kinds; ``after`` normalised by swapping earlier ↔ later).

causes → ``iso15926:CauseOfEvent``.
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

_OVERLAP_KINDS = {"during", "overlaps", "concurrent"}


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    for entry in data.get("temporal_relations") or []:
        tid = entry.get("id")
        earlier_id = entry.get("earlier")
        later_id   = entry.get("later")
        rel_kind   = entry.get("relation_kind") or "before"
        if not (tid and earlier_id and later_id):
            continue

        # Normalise "after" by swap so all sequences flow earlier → later.
        if rel_kind == "after":
            earlier_id, later_id = later_id, earlier_id
            rel_kind = "before"

        earlier = ctx.get(earlier_id)
        later   = ctx.get(later_id)
        if earlier is None or later is None:
            continue
        if earlier.kind != "activity" or later.kind != "activity":
            # Temporal relations only make sense between activity/event nodes
            # (which are both registered as kind="activity").
            continue

        uri = mint_ext(ctx.ext_ns, kind="tseq", ident=tid)
        if rel_kind == "causes":
            g.add((uri, RDF.type, ISO15926.CauseOfEvent))
            g.add((uri, P.CAUSE_CAUSER, earlier.uri))
            g.add((uri, P.CAUSE_CAUSED, later.uri))
        else:
            g.add((uri, RDF.type, ISO15926.TemporalSequence))
            g.add((uri, P.TEMPORAL_PREDECESSOR, earlier.uri))
            g.add((uri, P.TEMPORAL_SUCCESSOR,   later.uri))
            if rel_kind in _OVERLAP_KINDS:
                g.add((uri, DG.overlap, Literal(rel_kind)))

        if (desc := entry.get("description")):
            g.add((uri, DG.summary, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        ctx.register(EntityRef(id=tid, kind="temporal_relation", uri=uri,
                               label=f"{earlier.label} → {later.label}"))
    return g
