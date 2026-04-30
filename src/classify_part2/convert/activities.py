"""Convert prompt #2 (Activities & events) JSON output to Part 2 Turtle.

JSON shape::

    {"activities": [
        {"id":..., "label":..., "iso_class":"Activity"|"Event",
         "summary":..., "begin":...|null, "end":...|null,
         "evidence":...},
        ...
    ]}
"""

from __future__ import annotations

import re
from datetime import date, datetime

from rdflib import BNode, Graph, Literal, RDF, RDFS, URIRef, XSD

from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

_ISO_DATE_RE     = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def convert(data: dict, ctx: ConversionContext) -> Graph:
    """Emit Activity / Event triples and register each entity in ctx."""
    g = Graph()
    activities = data.get("activities") or []
    for entry in activities:
        _emit_activity(g, entry, ctx)
    return g


def _emit_activity(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    aid = entry.get("id")
    if not aid:
        return

    iso_class = entry.get("iso_class") or "Activity"
    cls = ISO15926.Event if iso_class == "Event" else ISO15926.Activity

    label = entry.get("label") or aid
    uri = mint_ext(ctx.ext_ns, kind="act", ident=aid)

    g.add((uri, RDF.type, cls))
    g.add((uri, RDFS.label, Literal(label)))

    if (summary := entry.get("summary")):
        g.add((uri, DG.summary, Literal(summary)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    _attach_temporal_bound(g, ctx, uri, ISO15926.Beginning, entry.get("begin"), suffix=f"{aid}-begin")
    _attach_temporal_bound(g, ctx, uri, ISO15926.Ending,    entry.get("end"),   suffix=f"{aid}-end")

    ctx.register(EntityRef(
        id=aid, kind="activity", uri=uri, label=label,
        summary=entry.get("summary") or "",
    ))


def _attach_temporal_bound(
    g: Graph,
    ctx: ConversionContext,
    activity_uri: URIRef,
    bound_class: URIRef,
    raw: str | None,
    *,
    suffix: str,
) -> None:
    """Reify a Beginning / Ending bound from a raw begin/end string."""
    if not raw:
        return

    bound_uri = mint_ext(ctx.ext_ns, kind="tb", ident=suffix)
    g.add((bound_uri, RDF.type, bound_class))

    typed = _typed_temporal_literal(raw)
    if typed is not None:
        g.add((bound_uri, DG.atTime, typed))
    else:
        # Natural-language date — preserve verbatim, mark unresolved.
        g.add((bound_uri, DG.atTime, Literal(raw)))
        g.add((bound_uri, DG.status, DG.Unresolved))

    g.add((activity_uri, DG.hasBound, bound_uri))


def _typed_temporal_literal(raw: str) -> Literal | None:
    """Try to coerce an ISO-8601 date or datetime to a typed literal."""
    s = raw.strip()
    if _ISO_DATETIME_RE.match(s):
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
            return Literal(s, datatype=XSD.dateTime)
        except ValueError:
            return None
    if _ISO_DATE_RE.match(s):
        try:
            date.fromisoformat(s)
            return Literal(s, datatype=XSD.date)
        except ValueError:
            return None
    return None
