"""Top-level classify pipeline.

Runs the 14-prompt sequence on a document's markdown content:

1. Prompt #1 (nature scan) is always run; its answers gate prompts 2-14.
2. The source document is typed by an ad-hoc ``ClassOfInformationObject``
   subclass minted from ``doc_kind``.
3. Each gated prompt runs in dependency order (see PIPELINE_ORDER).
   Each emits triples into the same per-document ``Graph`` and registers
   any new entities in the shared ``ConversionContext``.
4. The Graph is returned alongside metrics for storage on the extraction
   PROV activity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from rdflib import Graph, Literal, RDF, RDFS, URIRef, XSD
from rich.console import Console

from src.classify_part2 import convert, nature_scan, reify, runner
from src.classify_part2.context import ConversionContext
from src.classify_part2.nature_scan import NatureScanResult
from src.classify_part2.ns import DG, ISO15926
from src.models import ModelConfig

logger = logging.getLogger(__name__)


# (prompt-short-name, converter callable, max_tokens). Order is the
# dependency-respecting linear schedule from docs/classify_design.md.
PIPELINE_ORDER: list[tuple[str, callable, int]] = [
    ("activities_events",     convert.activities.convert,             4096),
    ("individuals",            convert.individuals.convert,            4096),
    ("classes_of_activity",    convert.classes.convert_activities,     4096),
    ("classes_of_individual",  convert.classes.convert_individuals,    4096),
    ("roles",                  convert.roles.convert,                  2048),
    ("participations",         convert.participations.convert,         4096),
    ("whole_parts",            convert.whole_parts.convert,            4096),
    ("temporal_relations",     convert.temporal.convert,               2048),
    ("properties",             convert.properties.convert_qualitative, 4096),
    ("quantities",             convert.properties.convert_quantitative,4096),
    ("identifiers",            convert.identifiers.convert,            4096),
    ("connections",            convert.connections.convert,            4096),
    ("lifecycle_approvals",    convert.lifecycle.convert,              4096),
]


@dataclass
class PipelineResult:
    """Everything needed by the caller to record the run on PROV."""
    graph:    Graph
    nature:   NatureScanResult
    ran:      list[str]      # prompt names that actually executed
    skipped:  list[str]      # prompt names skipped by gating
    started:  datetime
    ended:    datetime


def classify(
    *,
    markdown: str,
    ctx: ConversionContext,
    client,
    model: ModelConfig,
    console: Console,
) -> PipelineResult:
    """Run the 14-prompt classify pipeline against *markdown*.

    Triples are emitted into a fresh ``Graph`` and the caller (typically
    ``ingest_pdf``) merges it into the source's named extraction graph.
    """
    started = datetime.now(timezone.utc).replace(microsecond=0)
    g = Graph()
    g.bind("iso15926", ISO15926)
    g.bind("dg",       DG)

    # ── Prompt #1 — nature scan ──
    console.print("  [cyan]nature scan[/cyan]...")
    nat = nature_scan.run(markdown, client, model)
    ctx.doc_kind = nat.doc_kind
    ctx.primary_subjects = nat.primary_subjects
    console.print(
        f"    doc_kind: [bold]{nat.doc_kind or '(unspecified)'}[/bold]; "
        f"subjects: [dim]{', '.join(nat.primary_subjects) or '(none)'}[/dim]"
    )
    yes_keys = [k for k, a in nat.answers.items() if a.yes]
    console.print(f"    yes-answers ({len(yes_keys)}/11): [dim]{', '.join(yes_keys) or 'none'}[/dim]")
    console.print(
        f"    scope coverage: [bold]{nat.scope_coverage:.0%}[/bold]; "
        f"evidence coverage: [bold]{nat.evidence_coverage:.0%}[/bold]"
    )

    # ── Source document axes ──
    # Source is an actual, whole-life information-object instance. Stack
    # the same modal + perspective axes that convert/individuals.py applies
    # to P03-extracted entities (see iso_part2_coverage.md Finding 4). The
    # doc-kind class supplies the kind axis when nat.doc_kind is set.
    g.add((ctx.source_uri, RDF.type, ISO15926.ActualIndividual))
    g.add((ctx.source_uri, RDF.type, ISO15926.WholeLifeIndividual))
    if nat.doc_kind:
        # Plain rdf:type only — no reified Classification, since the
        # classification has no own metadata to carry.
        doc_class = reify.mint_class_of(
            g, ext_ns=ctx.ext_ns, label=nat.doc_kind,
            metaclass=ISO15926.ClassOfInformationObject,
            seen=ctx.classes_minted,
        )
        g.add((ctx.source_uri, RDF.type, doc_class))

    # ── Prompts #2-#14 ──
    gated = nature_scan.gating_decisions(nat)
    ran:     list[str] = []
    skipped: list[str] = []

    for name, conv_fn, max_tokens in PIPELINE_ORDER:
        if name not in gated:
            console.print(f"    [dim]skip {name}[/dim]")
            skipped.append(name)
            continue

        console.print(f"  [cyan]{name}[/cyan]...")
        try:
            data = runner.run(
                name, markdown=markdown, ctx=ctx,
                client=client, model=model, max_tokens=max_tokens,
            )
        except Exception as exc:
            console.print(f"    [yellow]{name} failed[/yellow]: {exc}")
            logger.exception("prompt %s failed", name)
            skipped.append(name)
            continue

        sub_g = conv_fn(data, ctx)
        g += sub_g
        ran.append(name)
        console.print(f"    {len(sub_g)} triple(s); ctx now [bold]{len(ctx.entities)}[/bold] entit(y/ies)")

    ended = datetime.now(timezone.utc).replace(microsecond=0)
    return PipelineResult(
        graph=g, nature=nat, ran=ran, skipped=skipped,
        started=started, ended=ended,
    )


def attach_pipeline_metrics(
    ds_default,
    *,
    ext_uri: URIRef,
    nat: NatureScanResult,
) -> None:
    """Attach coverage metrics from the nature scan to the extraction node."""
    ds_default.add((ext_uri, DG.scopeCoverage,
                    Literal(nat.scope_coverage,    datatype=XSD.decimal)))
    ds_default.add((ext_uri, DG.evidenceCoverage,
                    Literal(nat.evidence_coverage, datatype=XSD.decimal)))
    if nat.doc_kind:
        ds_default.add((ext_uri, DG.docKind, Literal(nat.doc_kind)))
