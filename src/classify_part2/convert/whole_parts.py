"""Convert prompt #8 (Whole-parts).

The prompt may introduce ``new_individuals`` or ``new_activities`` —
these are folded into the context so later prompts (#9, #10, #11, #13)
see them. Whole-part links are emitted as ``CompositionOfIndividual``
or ``TemporalWholePart`` / ``FeatureWholePart`` per ``relation_kind``.
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.convert import activities as conv_acts
from src.classify_part2.convert import individuals as conv_inds
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

_RELATION_TO_CLASS = {
    "spatial":       ISO15926.CompositionOfIndividual,
    "temporal":      ISO15926.TemporalWholePart,
    "feature":       ISO15926.FeatureWholePart,
    "informational": ISO15926.CompositionOfIndividual,
    "other":         ISO15926.CompositionOfIndividual,
}


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()

    # 1) Fold any new individuals/activities into the context first so
    # the whole-part loop can resolve them by id.
    new_inds = data.get("new_individuals") or []
    if new_inds:
        g += conv_inds.convert({"individuals": new_inds}, ctx)
    new_acts = data.get("new_activities") or []
    if new_acts:
        g += conv_acts.convert({"activities": new_acts}, ctx)

    # 2) Emit whole-part links.
    for entry in data.get("whole_parts") or []:
        wid = entry.get("id")
        whole_id = entry.get("whole")
        part_id  = entry.get("part")
        if not (wid and whole_id and part_id):
            continue
        whole = ctx.get(whole_id)
        part  = ctx.get(part_id)
        if whole is None or part is None:
            continue

        rel_kind = entry.get("relation_kind") or "spatial"
        wp_class = _RELATION_TO_CLASS.get(rel_kind, ISO15926.CompositionOfIndividual)

        uri = mint_ext(ctx.ext_ns, kind="wp", ident=wid)
        g.add((uri, RDF.type, wp_class))
        g.add((uri, P.COMPOSITION_WHOLE, whole.uri))
        g.add((uri, P.COMPOSITION_PART,  part.uri))
        if rel_kind == "informational":
            g.add((uri, RDFS.comment, Literal("informational")))
        if rel_kind == "other":
            g.add((uri, DG.status, DG.Unresolved))
            if (note := entry.get("note")):
                g.add((uri, RDFS.comment, Literal(note)))
        if (desc := entry.get("description")):
            g.add((uri, RDFS.comment, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        ctx.register(EntityRef(id=wid, kind="whole_part", uri=uri,
                               label=f"{whole.label} ⊃ {part.label}"))
    return g
