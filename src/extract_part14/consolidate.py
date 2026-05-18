"""Cross-doc consolidation of stable ext: classes.

The explicit operation that walks the RDL scope hierarchy and lifts
doc-local classes to project scope when N docs share the same slug
(this commit handles the slug-collision case; the next commit absorbs
the semantic compare from the old ext_dedup so different-slug
equivalents also fold). See docs/architecture/rdl-scopes.md.

`walk_consolidate()`:
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

Existing project-scope classes (already consolidated) aren't re-lifted.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

DCTERMS = Namespace("http://purl.org/dc/terms/")

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
class ConsolidationDecision:
    """One ext class promoted from per-doc scopes to project scope."""
    slug:           str
    canonical:      ExtClass            # merged canonical definition
    contributors:   list[str]           # doc slugs that contributed the declaration


def walk_consolidate(
    project_root: Path,
    *,
    threshold: int = 2,
    agent:     URIRef | None = None,
    timestamp: datetime | None = None,
    console=None,
) -> list[ConsolidationDecision]:
    """Scan + consolidate ext: classes used in ≥threshold docs.

    Returns the list of promotions made. Mutates files under project_root by
    writing one project-scope delta + one delta per contributing doc.
    """
    timestamp = timestamp or datetime.now(timezone.utc)
    deprecated_lit = Literal(True, datatype=XSD.boolean)

    # ── 1. Scan: build per-class list of contributing docs ──
    contributors_by_slug: dict[str, list[str]] = defaultdict(list)
    declarations_by_slug: dict[str, list[ExtClass]] = defaultdict(list)
    # follow_through: slug → docs whose doc-local class should be deprecated
    #   directly onto the upstream canonical, because the project-ext
    #   class for that slug is itself already retired (chain-following).
    follow_through: dict[str, list[str]] = defaultdict(list)
    doc_scopes_seen: list[str] = []

    project_state = materialize(project_root, project_scope())
    project_classes = extract_classes_from_graph(project_state)
    already_promoted = set(project_classes.keys())
    # Map: project-ext slug → upstream canonical it's been retired to.
    # Populated when a prior consolidate run's retire-upward pass marked
    # an ext: class deprecated with a `dcterms:isReplacedBy` pointer.
    deprecation_targets: dict[str, URIRef] = {}
    for slug, cls in project_classes.items():
        if (cls.uri, OWL.deprecated, deprecated_lit) not in project_state:
            continue
        target = next((o for o in project_state.objects(cls.uri, DCTERMS.isReplacedBy)
                       if isinstance(o, URIRef)), None)
        if target is not None:
            deprecation_targets[slug] = target

    for scope in list_scopes(project_root):
        if scope.kind != "doc" or not scope.name:
            continue
        doc_scopes_seen.append(scope.name)
        doc_state = materialize(project_root, scope)
        per_doc_classes = extract_classes_from_graph(doc_state)
        for slug, cls in per_doc_classes.items():
            # Skip already-deprecated doc-local classes (idempotency).
            if (cls.uri, OWL.deprecated, deprecated_lit) in doc_state:
                continue
            if slug in already_promoted:
                # If the project-ext class for this slug has itself been
                # retired upward to a wider RDL, follow the chain: this
                # doc-local URI should be deprecated DIRECTLY to the
                # upstream canonical, skipping the deprecated intermediate.
                if slug in deprecation_targets:
                    follow_through[slug].append(scope.name)
                continue
            contributors_by_slug[slug].append(scope.name)
            declarations_by_slug[slug].append(cls)

    # ── 2. Filter to stable classes ──
    decisions: list[ConsolidationDecision] = []
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
        decisions.append(ConsolidationDecision(
            slug=slug, canonical=canonical, contributors=contribs,
        ))

    if not decisions and not follow_through and console:
        console.print(f"  [dim]no ext class met threshold (≥{threshold} docs)[/dim]")

    # ── 3. Write project-scope additions (one delta) — only if there
    #    are new mint-upward decisions; follow-through and retire pass
    #    below don't add to project scope here (project-scope deprecation
    #    triples are emitted by retire-upward, instance retypes by both).
    if decisions:
        project_added = Graph()
        for d in decisions:
            for triple in class_definitions_graph([d.canonical]):
                project_added.add(triple)
            # Audit: which docs contributed this promotion
            for contrib in d.contributors:
                project_added.add((d.canonical.uri, DG.firstSeenIn,
                                   URIRef(f"urn:docgraph:scope/doc/{contrib}")))

        project_seq = next_seq(project_root, project_scope())
        write_delta(
            StepDelta(
                scope     = project_scope(),
                step      = "consolidate",
                seq       = project_seq,
                added     = project_added,
                parent_seq= project_seq - 1,
                agent     = agent,
                timestamp = timestamp,
            ),
            delta_path(project_root, project_scope(), project_seq),
        )

    # ── 4. Per-doc deltas — mark the doc-local class deprecated (W3C
    #    pattern: owl:deprecated + owl:equivalentClass + dcterms:isReplacedBy
    #    pointing at the canonical), AND rewrite instance type triples
    #    from the doc-local URI to the canonical. The doc-local class
    #    DEFINITION stays (lifecycle invariant). See
    #    docs/architecture/rdl-scopes.md.
    per_doc_removed: dict[str, Graph] = defaultdict(graph_with_ns_seed)
    per_doc_added:   dict[str, Graph] = defaultdict(graph_with_ns_seed)

    # Mint-upward: deprecate doc-local URIs onto the new project canonical.
    for d in decisions:
        for contrib in d.contributors:
            doc_local_uri = _doc_local_uri(contrib, d.slug)
            apply_deprecation_to_doc(
                project_root, contrib, doc_local_uri, d.canonical.uri,
                per_doc_added[contrib], per_doc_removed[contrib],
                deprecated_lit,
            )

    # Chain-following: doc-local slugs whose project-ext canonical has
    # itself been retired upward (Scenario A in rdl-scopes.md). Deprecate
    # them DIRECTLY to the upstream canonical, skipping the deprecated
    # intermediate. Same delta-shape as mint-upward.
    for slug, contribs in follow_through.items():
        upstream_uri = deprecation_targets[slug]
        for contrib in contribs:
            doc_local_uri = _doc_local_uri(contrib, slug)
            apply_deprecation_to_doc(
                project_root, contrib, doc_local_uri, upstream_uri,
                per_doc_added[contrib], per_doc_removed[contrib],
                deprecated_lit,
            )

    contributing_slugs = set(per_doc_removed) | set(per_doc_added)
    for slug in contributing_slugs:
        scope = doc_scope(slug)
        seq   = next_seq(project_root, scope)
        write_delta(
            StepDelta(
                scope     = scope,
                step      = "consolidate",
                seq       = seq,
                added     = per_doc_added.get(slug, Graph()),
                removed   = per_doc_removed.get(slug, Graph()),
                parent_seq= seq - 1,
                agent     = agent,
                timestamp = timestamp,
            ),
            delta_path(project_root, scope, seq),
        )

    if console:
        for d in decisions:
            console.print(f"    [dim]promoted ext:{d.slug}  "
                          f"({len(d.contributors)} contributors: "
                          f"{', '.join(d.contributors)})[/dim]")
        for slug, contribs in follow_through.items():
            console.print(f"    [dim]chain-followed {slug} → "
                          f"{deprecation_targets[slug]} "
                          f"({len(contribs)} doc(s))[/dim]")

    # ── 5. Retire-upward pass — find project-ext classes whose slug also
    #    exists in an upstream RDL (LIS-14, dg, any registered third-
    #    party RDL). Emit deprecation triples on the project-ext class
    #    pointing at the upstream URI, and rewrite contributing-doc
    #    instance triples from `ext:Foo` to `<upstream>:Foo`. Slug-match
    #    only today; embedding+LLM semantic compare is a follow-up.
    upstream_view = _project_with_upstream(project_root)
    if upstream_view is not None:
        _retire_upward(
            project_root, ontology_view=upstream_view,
            agent=agent, timestamp=timestamp, console=console,
        )

    return decisions


def _project_with_upstream(project_root: Path) -> Graph | None:
    """Materialize project scope + all docs into one Graph view that
    also contains upstream RDL classes. Used by `_retire_upward` to
    spot upstream slugs (LIS-14, dg vocab, etc.) without re-loading
    the foundationals from disk.

    Returns None when the project isn't fully initialised (no config.ttl,
    e.g. test fixtures that only seed bare deltas). The retire-upward
    pass is then skipped — it's a best-effort enhancement on top of
    mint-upward, not a precondition for it.
    """
    try:
        from src.extract_part14.loader import build_dataset, union_view
        ds = build_dataset(project_root)
        return union_view(ds)
    except FileNotFoundError:
        return None


def _retire_upward(
    project_root: Path,
    *,
    ontology_view: Graph,
    agent:     URIRef | None,
    timestamp: datetime,
    console=None,
) -> int:
    """Find project-ext classes that have a same-slug equivalent in an
    upstream RDL; emit deprecation triples + rewrite contributing-doc
    instance triples. Returns the number of classes retired."""
    project_state = materialize(project_root, project_scope())
    deprecated_lit = Literal(True, datatype=XSD.boolean)

    retires: list[tuple[ExtClass, URIRef]] = []
    for slug, cls in extract_classes_from_graph(project_state).items():
        if cls.namespace != EXT:
            continue                                # only retire project-ext
        if (cls.uri, OWL.deprecated, deprecated_lit) in project_state:
            continue                                # idempotency
        upstream_uri = _find_upstream_class_by_slug(ontology_view, slug, exclude=cls.uri)
        if upstream_uri is not None:
            retires.append((cls, upstream_uri))

    if not retires:
        return 0

    # Project-scope delta: deprecation triples on each retired class.
    project_added = graph_with_ns_seed()
    for cls, upstream_uri in retires:
        project_added.add((cls.uri, OWL.deprecated,        deprecated_lit))
        project_added.add((cls.uri, OWL.equivalentClass,   upstream_uri))
        project_added.add((cls.uri, DCTERMS.isReplacedBy,  upstream_uri))

    project_seq = next_seq(project_root, project_scope())
    write_delta(
        StepDelta(
            scope     = project_scope(),
            step      = "consolidate",
            seq       = project_seq,
            added     = project_added,
            parent_seq= project_seq - 1,
            agent     = agent,
            timestamp = timestamp,
        ),
        delta_path(project_root, project_scope(), project_seq),
    )

    # Per-doc deltas: rewrite instance triples typed as the retired URI
    # to the upstream URI. Pure object replacement; the deprecated URI
    # itself is still resolvable via the equivalentClass triple.
    retire_map = {cls.uri: upstream for cls, upstream in retires}
    per_doc_removed: dict[str, Graph] = defaultdict(graph_with_ns_seed)
    per_doc_added:   dict[str, Graph] = defaultdict(graph_with_ns_seed)

    for scope in list_scopes(project_root):
        if scope.kind != "doc" or not scope.name:
            continue
        doc_state = materialize(project_root, scope)
        for retired_uri, upstream_uri in retire_map.items():
            for s in doc_state.subjects(RDF.type, retired_uri):
                per_doc_removed[scope.name].add((s, RDF.type, retired_uri))
                per_doc_added[scope.name].add((s, RDF.type, upstream_uri))

    for slug_doc in set(per_doc_removed) | set(per_doc_added):
        scope = doc_scope(slug_doc)
        seq   = next_seq(project_root, scope)
        write_delta(
            StepDelta(
                scope     = scope,
                step      = "consolidate",
                seq       = seq,
                added     = per_doc_added.get(slug_doc, Graph()),
                removed   = per_doc_removed.get(slug_doc, Graph()),
                parent_seq= seq - 1,
                agent     = agent,
                timestamp = timestamp,
            ),
            delta_path(project_root, scope, seq),
        )

    if console:
        for cls, upstream_uri in retires:
            console.print(f"    [dim]retired ext:{cls.slug} → {upstream_uri}[/dim]")
    return len(retires)


def _find_upstream_class_by_slug(
    ontology: Graph, slug: str, *, exclude: URIRef,
) -> URIRef | None:
    """Look up an owl:Class whose local-name part matches *slug* and
    that lives outside of docgraph's own namespaces (doc-local source +
    project ext:). Returns the upstream URI or None.

    Matches against any registered upstream RDL — LIS-14, dg vocab,
    PROV, any third-party ontology the loader has pulled in. Caller
    passes the URI of the class being checked via *exclude* so a class
    can't "retire" to itself.
    """
    SOURCE_PREFIX = "urn:docgraph:source:"
    EXT_PREFIX    = str(EXT)
    for cls_uri in ontology.subjects(RDF.type, OWL.Class):
        if not isinstance(cls_uri, URIRef) or cls_uri == exclude:
            continue
        s = str(cls_uri)
        if s.startswith(SOURCE_PREFIX) or s.startswith(EXT_PREFIX):
            continue
        local = s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if local == slug:
            return cls_uri
    return None


def apply_deprecation_to_doc(
    project_root: Path,
    doc_slug: str,
    doc_local_uri: URIRef,
    canonical_uri: URIRef,
    added_g: Graph,
    removed_g: Graph,
    deprecated_lit: Literal,
) -> None:
    """Add the deprecation triple set on *doc_local_uri* pointing at
    *canonical_uri*, and rewrite that doc's instance triples typed as
    *doc_local_uri* to type as *canonical_uri* instead. Mutates the
    caller-supplied added/removed graphs (per-doc accumulators)."""
    added_g.add((doc_local_uri, OWL.deprecated,        deprecated_lit))
    added_g.add((doc_local_uri, OWL.equivalentClass,   canonical_uri))
    added_g.add((doc_local_uri, DCTERMS.isReplacedBy,  canonical_uri))
    doc_state = materialize(project_root, doc_scope(doc_slug))
    for s in doc_state.subjects(RDF.type, doc_local_uri):
        removed_g.add((s, RDF.type, doc_local_uri))
        added_g.add((s, RDF.type, canonical_uri))


def graph_with_ns_seed() -> Graph:
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
