"""Cross-doc promotion of stable ext: classes.

When the same class slug shows up in multiple docs (e.g. doc 1 proposed
`Invoice` under its own namespace, and doc 2 did too), the class is
"stable" — used widely enough to deserve a canonical home in the
project-scope ext: ontology rather than buried in whichever docs
first proposed it.

`walk_promote()`:
  1. Scans every per-doc graph for ext-class declarations (matched by
     `dg:provenance` marker, not URI prefix — so per-doc-namespaced
     proposals are picked up).
  2. Counts the distinct docs that DECLARE each slug.
  3. For slugs meeting the threshold (default N≥2), builds a canonical
     definition at the project EXT namespace by merging the duplicate
     declarations.
  4. Emits a project-scope delta that ADDS the canonical definition
     (URI: `urn:docgraph:vocab:ext#<Slug>`, provenance "promoted").
  5. Emits per-doc-scope deltas that REMOVE the per-doc class
     declaration AND rewrite per-doc `<entity> rdf:type <doc-ns>/<Slug>`
     into `<entity> rdf:type ext:<Slug>` so instances stay typed
     against the now-canonical project URI.

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

from rdflib import Graph, Literal, Namespace, URIRef
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

SOURCE_NS = Namespace("urn:docgraph:source:")


def _doc_local_uri(doc_slug: str, class_slug: str) -> URIRef:
    """The URI a doc-local ext class lives at — ``urn:docgraph:source:<doc>/<Class>``."""
    return URIRef(f"{SOURCE_NS}{doc_slug}/{class_slug}")

logger = logging.getLogger(__name__)


@dataclass
class PromotionDecision:
    """One ext class promoted from per-doc scopes to project scope."""
    slug:           str
    canonical:      ExtClass            # merged canonical definition
    contributors:   list[str]           # doc slugs that contributed the declaration


def walk_promote(
    project_root: Path,
    *,
    threshold: int = 2,
    agent:     URIRef | None = None,
    timestamp: datetime | None = None,
    console=None,
) -> list[PromotionDecision]:
    """Scan + promote ext: classes used in ≥threshold docs.

    Returns the list of promotions made. Mutates files under project_root by
    writing one project-scope delta + one delta per contributing doc.
    """
    timestamp = timestamp or datetime.now(timezone.utc)
    # ── 1. Scan: build per-class list of contributing docs ──
    contributors_by_slug: dict[str, list[str]] = defaultdict(list)
    declarations_by_slug: dict[str, list[ExtClass]] = defaultdict(list)
    doc_scopes_seen: list[str] = []

    project_state = materialize(project_root, project_scope())
    already_promoted = set(extract_classes_from_graph(project_state).keys())

    for scope in list_scopes(project_root):
        if scope.kind != "doc" or not scope.name:
            continue
        doc_scopes_seen.append(scope.name)
        doc_state = materialize(project_root, scope)
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
        # Promotion moves the class into the project EXT namespace and
        # stamps provenance = "promoted" (regardless of the per-doc
        # source markers).
        canonical = ExtClass(
            slug       = merged.slug,
            anchor     = merged.anchor,
            label      = merged.label,
            alt_labels = merged.alt_labels,
            comment    = merged.comment,
            provenance = "promoted",
            first_seen = merged.first_seen,
            namespace  = EXT,
        )
        decisions.append(PromotionDecision(
            slug=slug, canonical=canonical, contributors=contribs,
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

    project_seq = next_seq(project_root, project_scope())
    project_delta = StepDelta(
        scope     = project_scope(),
        step      = "promote",
        seq       = project_seq,
        added     = project_added,
        parent_seq= project_seq - 1,
        agent     = agent,
        timestamp = timestamp,
    )
    write_delta(project_delta, delta_path(project_root, project_scope(), project_seq))

    # ── 4. Per-doc deltas — drop the per-doc class definition AND
    #    rewrite instance type triples from the doc-local class URI
    #    to the project-scope canonical URI (so instances stay typed).
    per_doc_removed: dict[str, Graph] = defaultdict(_graph_with_ns_seed)
    per_doc_added:   dict[str, Graph] = defaultdict(_graph_with_ns_seed)

    for d in decisions:
        for contrib in d.contributors:
            doc_local_uri = _doc_local_uri(contrib, d.slug)
            # Remove the doc-local class definition (all triples about it).
            doc_state = materialize(project_root, doc_scope(contrib))
            for s, p, o in doc_state.triples((doc_local_uri, None, None)):
                per_doc_removed[contrib].add((s, p, o))
            # Rewrite instance type triples: rdf:type doc_local_uri →
            # rdf:type canonical.uri.
            for s in doc_state.subjects(RDF.type, doc_local_uri):
                per_doc_removed[contrib].add((s, RDF.type, doc_local_uri))
                per_doc_added[contrib].add((s, RDF.type, d.canonical.uri))

    contributing_slugs = set(per_doc_removed) | set(per_doc_added)
    for slug in contributing_slugs:
        scope = doc_scope(slug)
        seq   = next_seq(project_root, scope)
        delta = StepDelta(
            scope     = scope,
            step      = "promote",
            seq       = seq,
            added     = per_doc_added.get(slug, Graph()),
            removed   = per_doc_removed.get(slug, Graph()),
            parent_seq= seq - 1,
            agent     = agent,
            timestamp = timestamp,
        )
        write_delta(delta, delta_path(project_root, scope, seq))

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
