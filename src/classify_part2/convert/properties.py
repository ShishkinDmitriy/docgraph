"""Convert prompts #10 (qualitative Properties) and #11 (Quantities).

Both produce ``iso15926:Property`` instances classified by an ad-hoc
``ClassOfProperty`` (one per distinct ``property_kind`` /
``quantity_kind`` string). Quantities additionally carry a ``Scale``
and a numeric value (or PropertyRange for bounded values).
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS, URIRef

from src.classify_part2 import owl_props as P
from src.classify_part2 import reify
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

_BEARER_KINDS = {
    "individual",
    "activity",
    "class_of_individual",
    "class_of_activity",
}


def convert_qualitative(data: dict, ctx: ConversionContext) -> Graph:
    """Prompt #10."""
    g = Graph()
    for entry in data.get("properties") or []:
        pid = entry.get("id")
        bearer_id = entry.get("bearer")
        bearer_kind = entry.get("bearer_kind")
        prop_kind = entry.get("property_kind")
        value = entry.get("value")
        if not (pid and bearer_id and prop_kind and value is not None):
            continue
        bearer = _resolve_bearer(ctx, bearer_id, bearer_kind)
        if bearer is None:
            continue

        cls_uri = reify.mint_class_of(
            g, ext_ns=ctx.ext_ns,
            label=_titlecase(prop_kind),
            metaclass=ISO15926.ClassOfProperty,
            seen=ctx.classes_minted,
        )
        uri = mint_ext(ctx.ext_ns, kind="prop", ident=pid)
        g.add((uri, RDF.type, ISO15926.Property))
        g.add((uri, RDF.type, cls_uri))
        g.add((uri, P.PROPERTY_POSSESSOR, bearer.uri))
        g.add((uri, RDFS.label, Literal(str(value))))
        if (desc := entry.get("description")):
            g.add((uri, RDFS.comment, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        ctx.register(EntityRef(id=pid, kind="property", uri=uri,
                               label=f"{prop_kind}={value}"))
    return g


def convert_quantitative(data: dict, ctx: ConversionContext) -> Graph:
    """Prompt #11."""
    g = Graph()
    for entry in data.get("quantities") or []:
        qid = entry.get("id")
        bearer_id = entry.get("bearer")
        bearer_kind = entry.get("bearer_kind")
        kind = entry.get("quantity_kind")
        if not (qid and bearer_id and kind):
            continue
        bearer = _resolve_bearer(ctx, bearer_id, bearer_kind)
        if bearer is None:
            continue

        cls_uri = reify.mint_class_of(
            g, ext_ns=ctx.ext_ns,
            label=_titlecase(kind),
            metaclass=ISO15926.ClassOfProperty,
            seen=ctx.classes_minted,
        )
        scale_uri = reify.mint_scale(
            g, ext_ns=ctx.ext_ns,
            raw_unit=entry.get("unit"),
            seen=ctx.scales_minted,
        )

        uri = mint_ext(ctx.ext_ns, kind="qty", ident=qid)
        g.add((uri, RDF.type, ISO15926.Property))
        g.add((uri, RDF.type, cls_uri))
        g.add((uri, P.PROPERTY_POSSESSOR, bearer.uri))
        if scale_uri is not None:
            g.add((uri, DG.onScale, scale_uri))

        exact = entry.get("exact")
        minv  = entry.get("min")
        maxv  = entry.get("max")
        if exact is not None:
            g.add((uri, P.HAS_CONTENT, Literal(str(exact))))
        elif minv is not None or maxv is not None:
            rng = reify.add_property_range(
                g, ext_ns=ctx.ext_ns, prop=uri,
                minimum=str(minv) if minv is not None else None,
                maximum=str(maxv) if maxv is not None else None,
            )
            g.add((uri, DG.hasRange, rng))

        if (desc := entry.get("description")):
            g.add((uri, RDFS.comment, Literal(desc)))
        if (evidence := entry.get("evidence")):
            g.add((uri, DG.evidence, Literal(evidence)))

        ctx.register(EntityRef(id=qid, kind="quantity", uri=uri,
                               label=f"{kind} of {bearer.label}"))
    return g


def _resolve_bearer(ctx: ConversionContext, bearer_id: str, bearer_kind: str | None):
    if bearer_kind not in _BEARER_KINDS:
        bearer_kind = None
    ref = ctx.get(bearer_id)
    if ref is None:
        return None
    if bearer_kind and ref.kind != bearer_kind:
        return None
    return ref


def _titlecase(s: str) -> str:
    """flow_rate → Flow Rate; pressure → Pressure."""
    return " ".join(p.capitalize() for p in s.replace("-", "_").split("_") if p)
