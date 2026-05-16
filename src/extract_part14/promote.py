"""Cross-doc promotion of stable ext: classes.

When the same ext: class is declared in multiple docs (e.g. doc 1
proposed `ext:Invoice` and doc 2's dedup landed instances under that
same URI), the class is "stable" — used widely enough to deserve a
canonical home in the project-scope ontology rather than buried in
whichever doc first proposed it.

`walk_promote()`:
  1. Scans every per-doc graph for `ext:<slug> rdf:type owl:Class`
     declarations.
  2. Counts the distinct docs that DECLARE each class.
  3. For classes meeting the threshold (default N≥2), builds a
     canonical definition by merging the duplicate declarations
     (longest label/comment wins, altLabels union — same logic
     `extract_classes_from_graph` already uses).
  4. Emits a project-scope delta that ADDS the canonical definition.
  5. Emits per-doc-scope deltas that REMOVE the class declarations
     from each contributing doc (the URI is the same, so instances
     in those docs continue to type correctly — they're now pointing
     at a class defined in project scope).

Instance triples (`<entity> rdf:type ext:Foo`) stay in their docs;
only the class metadata moves.

Existing project-scope classes (already promoted) aren't re-promoted.

Pure mechanical pass — no LLM. The dedup phase did the LLM-aided
semantic dedup upstream; promote just consolidates the result.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS

from src.deltas import (
    StepDelta,
    copy_namespaces,
    delta_path,
    doc_scope,
    list_scopes,
    materialize,
    next_seq,
    project_scope,
    write_delta,
)
from src.extract_part14.ext_ontology import (
    DG,
    EXT,
    ExtClass,
    class_definitions_graph,
    extract_classes_from_graph,
)

logger = logging.getLogger(__name__)


@dataclass
class PromotionDecision:
    """One ext class promoted from per-doc scopes to project scope."""
    slug:           str
    canonical:      ExtClass            # merged canonical definition
    contributors:   list[str]           # doc slugs that contributed the declaration


def walk_promote(
    graphs_dir: Path,
    *,
    threshold: int = 2,
    agent:     URIRef | None = None,
    timestamp: datetime | None = None,
    console=None,
) -> list[PromotionDecision]:
    """Scan + promote ext: classes used in ≥threshold docs.

    Returns the list of promotions made. Mutates the graphs_dir by
    writing one project-scope delta + one delta per contributing doc.
    """
    timestamp = timestamp or datetime.now(timezone.utc)
    # ── 1. Scan: build per-class list of contributing docs ──
    contributors_by_slug: dict[str, list[str]] = defaultdict(list)
    declarations_by_slug: dict[str, list[ExtClass]] = defaultdict(list)
    doc_scopes_seen: list[str] = []

    project_state = materialize(graphs_dir, project_scope())
    already_promoted = set(extract_classes_from_graph(project_state).keys())

    for scope in list_scopes(graphs_dir):
        if scope.kind != "doc" or not scope.name:
            continue
        doc_scopes_seen.append(scope.name)
        doc_state = materialize(graphs_dir, scope)
        per_doc_classes = extract_classes_from_graph(doc_state)
        for slug, cls in per_doc_classes.items():
            if slug in already_promoted:
                continue
            contributors_by_slug[slug].append(scope.name)
            declarations_by_slug[slug].append(cls)

    # ── 2. Filter to stable classes ──
    decisions: list[PromotionDecision] = []
    for slug, contribs in contributors_by_slug.items():
        if len(contribs) < threshold:
            continue
        # Canonical = merge all contributing declarations. Use a fresh
        # extract on a graph containing only those declarations so
        # extract_classes_from_graph's merge logic (longest label,
        # union altLabels) runs.
        merge_graph = Graph()
        for cls in declarations_by_slug[slug]:
            for triple in class_definitions_graph([cls]):
                merge_graph.add(triple)
        merged = extract_classes_from_graph(merge_graph).get(slug)
        if merged is None:
            continue
        decisions.append(PromotionDecision(
            slug=slug, canonical=merged, contributors=contribs,
        ))

    if not decisions:
        if console:
            console.print(f"  [dim]no ext class met threshold (≥{threshold} docs)[/dim]")
        return []

    # ── 3. Write project-scope additions (one delta) ──
    project_added = Graph()
    for d in decisions:
        for triple in class_definitions_graph([d.canonical]):
            project_added.add(triple)
        # Audit: which docs contributed this promotion
        for contrib in d.contributors:
            project_added.add((d.canonical.uri, DG.firstSeenIn,
                               URIRef(f"urn:docgraph:scope/doc/{contrib}")))

    project_seq = next_seq(graphs_dir, project_scope())
    project_delta = StepDelta(
        scope     = project_scope(),
        step      = "promote",
        seq       = project_seq,
        added     = project_added,
        parent_seq= project_seq - 1,
        agent     = agent,
        timestamp = timestamp,
    )
    write_delta(project_delta, delta_path(graphs_dir, project_scope(), project_seq))

    # ── 4. Per-doc removals — drop the class definition triples
    #    from each contributing doc's scope (instance triples remain).
    per_doc_to_remove: dict[str, Graph] = defaultdict(_graph_with_ns_seed)
    for d in decisions:
        for triple in class_definitions_graph([d.canonical]):
            for contrib in d.contributors:
                per_doc_to_remove[contrib].add(triple)

    for slug, removal_graph in per_doc_to_remove.items():
        scope = doc_scope(slug)
        seq   = next_seq(graphs_dir, scope)
        delta = StepDelta(
            scope     = scope,
            step      = "promote",
            seq       = seq,
            added     = Graph(),
            removed   = removal_graph,
            parent_seq= seq - 1,
            agent     = agent,
            timestamp = timestamp,
        )
        write_delta(delta, delta_path(graphs_dir, scope, seq))

    if console:
        for d in decisions:
            console.print(f"    [dim]promoted ext:{d.slug}  "
                          f"({len(d.contributors)} contributors: "
                          f"{', '.join(d.contributors)})[/dim]")

    return decisions


def _graph_with_ns_seed() -> Graph:
    """Default factory for the per-doc removal Graphs. Seeds the ext
    namespace so the resulting delta file's @prefix declarations are
    populated (rule: namespaces must propagate to serialization)."""
    g = Graph()
    g.bind("ext",  EXT)
    g.bind("dg",   DG)
    g.bind("rdfs", RDFS)
    g.bind("skos", SKOS)
    g.bind("owl",  OWL)
    return g
