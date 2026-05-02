"""Convert prompt #2 (Activities & events) JSON output to Part 2 Turtle.

Each begin/end produces three nodes per Part 2 §5.2.9:

  <act/X>           a iso15926:Activity ; rdfs:label "..." .
  <tb/X-begin>      a iso15926:Beginning ;          # IS-A CompositionOfIndividual
                    iso15926:hasWhole <act/X> ;     # the activity bounded
                    iso15926:hasPart  <time/X-begin> .  # the time it occurs
  <time/X-begin>    a iso15926:PointInTime ;
                    iso15926:hasContent "2025-01-17"^^xsd:date .

Natural-language times that don't parse to ISO-8601 produce a
``PeriodInTime`` instead of ``PointInTime`` and get ``dg:status
dg:Unresolved`` so callers can find them.

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

from rdflib import Graph, Literal, RDF, RDFS, URIRef, XSD

from src.classify_part2 import owl_props as P
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext, slugify

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
        g.add((uri, RDFS.comment, Literal(summary)))
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
    """Mint a Beginning/Ending + PointInTime/PeriodInTime per Part 2 §5.2.9.

    Beginning ``rdfs:subClassOf`` CompositionOfIndividual: the bound's
    *whole* is the activity, the *part* is the time individual.
    """
    if not raw:
        return

    bound_uri = mint_ext(ctx.ext_ns, kind="tb", ident=suffix)
    g.add((bound_uri, RDF.type, bound_class))
    g.add((bound_uri, P.COMPOSITION_WHOLE, activity_uri))

    time_uri = mint_ext(ctx.ext_ns, kind="time", ident=f"{suffix}-{slugify(raw)}")
    typed = _typed_temporal_literal(raw)
    if typed is not None:
        g.add((time_uri, RDF.type, ISO15926.PointInTime))
        g.add((time_uri, P.HAS_CONTENT, typed))
    else:
        # Natural-language phrase — preserve verbatim, mark unresolved.
        g.add((time_uri, RDF.type, ISO15926.PeriodInTime))
        g.add((time_uri, P.HAS_CONTENT, Literal(raw)))
        g.add((time_uri, DG.status, DG.Unresolved))

    g.add((bound_uri, P.COMPOSITION_PART, time_uri))


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
