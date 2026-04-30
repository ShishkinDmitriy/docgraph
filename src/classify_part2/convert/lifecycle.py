"""Convert prompt #14 (Lifecycle & approvals).

Three parallel lists (approvals, lifecycle_stages, revisions). Each
produces a small reified node:

- ``Approval`` + ``hasApprover`` + ``hasApproved`` + ``ClassOfApprovalByStatus``
- ``LifecycleStage`` + ``hasInterest`` + ``ClassOfLifecycleStage``
- ``Identification`` + ``dg:supersedes`` (no Part 2 Revision class)
"""

from __future__ import annotations

import re
from datetime import date, datetime

from rdflib import Graph, Literal, RDF, RDFS, URIRef, XSD

from src.classify_part2 import owl_props as P
from src.classify_part2 import reify
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext

_ISO_DATE_RE     = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    for entry in data.get("approvals") or []:
        _emit_approval(g, entry, ctx)
    for entry in data.get("lifecycle_stages") or []:
        _emit_lifecycle(g, entry, ctx)
    for entry in data.get("revisions") or []:
        _emit_revision(g, entry, ctx)
    return g


def _emit_approval(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    aid = entry.get("id")
    subject_id = entry.get("subject")
    status = entry.get("status") or "approved"
    if not (aid and subject_id):
        return
    subject = ctx.get(subject_id)
    if subject is None:
        return

    uri = mint_ext(ctx.ext_ns, kind="appr", ident=aid)
    g.add((uri, RDF.type, ISO15926.Approval))
    g.add((uri, P.APPROVAL_APPROVED, subject.uri))

    # Status as a ClassOfApprovalByStatus subclass keyed by label.
    status_uri = reify.mint_class_of(
        g, ext_ns=ctx.ext_ns,
        label=status.capitalize(),
        metaclass=ISO15926.ClassOfApprovalByStatus,
        seen=ctx.classes_minted,
    )
    g.add((uri, DG.approvalStatus, status_uri))

    if (by_id := entry.get("by")):
        by_ref = ctx.get(by_id)
        if by_ref:
            g.add((uri, P.APPROVAL_APPROVER, by_ref.uri))

    _add_when(g, uri, entry.get("when"))
    if (desc := entry.get("description")):
        g.add((uri, DG.summary, Literal(desc)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    ctx.register(EntityRef(id=aid, kind="approval", uri=uri,
                           label=f"{status} {subject.label}"))


def _emit_lifecycle(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    lid = entry.get("id")
    subject_id = entry.get("subject")
    stage = entry.get("stage")
    if not (lid and subject_id and stage):
        return
    subject = ctx.get(subject_id)
    if subject is None:
        return

    uri = mint_ext(ctx.ext_ns, kind="ls", ident=lid)
    g.add((uri, RDF.type, ISO15926.LifecycleStage))
    g.add((uri, P.LIFECYCLE_INTEREST, subject.uri))

    stage_uri = reify.mint_class_of(
        g, ext_ns=ctx.ext_ns,
        label=_titlecase(stage),
        metaclass=ISO15926.ClassOfLifecycleStage,
        seen=ctx.classes_minted,
    )
    g.add((uri, RDF.type, stage_uri))

    _add_when(g, uri, entry.get("when"))
    if (desc := entry.get("description")):
        g.add((uri, DG.summary, Literal(desc)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    ctx.register(EntityRef(id=lid, kind="lifecycle_stage", uri=uri,
                           label=f"{stage} of {subject.label}"))


def _emit_revision(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    rid = entry.get("id")
    subject_id = entry.get("subject")
    version = entry.get("version")
    if not (rid and subject_id and version):
        return
    subject = ctx.get(subject_id)
    if subject is None:
        return

    uri = mint_ext(ctx.ext_ns, kind="rev", ident=rid)
    g.add((uri, RDF.type, ISO15926.Identification))
    g.add((uri, P.REPR_REPRESENTED, subject.uri))
    g.add((uri, DG.value, Literal(str(version))))
    g.add((uri, DG.system, Literal("revision_label")))

    if (sup := entry.get("supersedes")):
        g.add((uri, DG.supersedes, Literal(str(sup))))

    _add_when(g, uri, entry.get("when"))
    if (desc := entry.get("description")):
        g.add((uri, DG.summary, Literal(desc)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    ctx.register(EntityRef(id=rid, kind="revision", uri=uri,
                           label=f"{version} of {subject.label}"))


def _add_when(g: Graph, uri: URIRef, when: str | None) -> None:
    if not when:
        return
    s = when.strip()
    if _ISO_DATETIME_RE.match(s):
        try:
            datetime.fromisoformat(s.replace("Z", "+00:00"))
            g.add((uri, DG.atTime, Literal(s, datatype=XSD.dateTime)))
            return
        except ValueError:
            pass
    if _ISO_DATE_RE.match(s):
        try:
            date.fromisoformat(s)
            g.add((uri, DG.atTime, Literal(s, datatype=XSD.date)))
            return
        except ValueError:
            pass
    g.add((uri, DG.atTime, Literal(s)))
    g.add((uri, DG.status, DG.Unresolved))


def _titlecase(s: str) -> str:
    return " ".join(p.capitalize() for p in s.replace("-", "_").split("_") if p)
