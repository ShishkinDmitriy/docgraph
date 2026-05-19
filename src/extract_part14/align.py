"""Per-doc alignment to higher-scope classes.

The instance-retyping operation per docs/architecture/rdl-scopes.md:
for each doc-local ext class proposed by the mega-walker, find the
highest-scope class with the same slug (project ext: → upstream RDLs
like LIS-14 / dg vocab / registered third-party ontologies). When a
match is found, deprecate the doc-local URI and retype instances
directly to the higher-scope canonical.

Single-doc invariant: after `align_doc` runs, the doc graph contains
NO doc-local class that has a higher-scope equivalent — alignment
prevents the single-doc inconsistency where the LLM mints a class
for something the project (or its upstream RDLs) already names.

Runs automatically at the end of the `add` pipeline so single-doc
ingests are self-consistent on the first pass. Also exposed as a
standalone entry point so `docgraph enrich` (after this rolls into
its scope-walking refactor) and other tooling can invoke it.

Cross-doc duplication — when two docs propose the same slug
independently and neither matches an existing higher-scope class — is
NOT alignment's job. That's `docgraph consolidate`'s mint-upward
pass. The two operations are intentionally complementary: alignment
prevents intra-doc redundancy at ingest, consolidate handles cross-
doc convergence on demand.

Slug-match only today (cheap, deterministic, handles the typical
case). Embedding + LLM relation classifier for different-slug
semantic equivalents (BankAccount ≡ IBAN) is the natural follow-up;
it would extend `_find_higher_scope_match` with a fallback branch.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, XSD

from src.deltas import (
    StepDelta,
    delta_path,
    doc_scope,
    materialize,
    next_seq,
    project_scope,
    write_delta,
)
from src.extract_part14.consolidate import (
    EXT,
    DCTERMS,
    apply_deprecation_to_doc,
    graph_with_ns_seed,
)
from src.sources import SOURCE_NS
from src.extract_part14.ext_ontology import (
    ExtClass,
    extract_classes_from_graph,
)

logger = logging.getLogger(__name__)


def align_doc(
    project_root: Path,
    slug: str,
    *,
    ontology: Graph | None = None,
    agent:    URIRef | None = None,
    timestamp: datetime | None = None,
    console=None,
) -> int:
    """Align *slug* doc's doc-local ext classes to the highest-scope
    equivalent. Returns the number of classes aligned.

    Writes a single doc-scope delta (step="align") containing:
      - Deprecation triples on each aligned doc-local URI pointing at
        the higher-scope canonical (owl:deprecated + owl:equivalentClass
        + dcterms:isReplacedBy).
      - Instance retypes (rdf:type doc-local → rdf:type canonical) for
        every entity in the doc typed at the deprecated URI.

    Idempotent: re-running skips doc-local classes already marked
    owl:deprecated.

    *ontology* is the loader's union view (used to discover upstream
    RDL classes). If None, the function tries to build it; on failure
    (e.g. test fixtures without config.ttl) alignment is a no-op and
    returns 0.
    """
    if ontology is None:
        ontology = _project_union_view(project_root)
    if ontology is None:
        return 0

    timestamp = timestamp or datetime.now(timezone.utc)
    deprecated_lit = Literal(True, datatype=XSD.boolean)

    # Higher-scope candidates: every owl:Class outside doc-local source
    # namespaces, indexed by local name (slug).
    upstream_by_slug = _index_higher_scope_classes(ontology)
    if not upstream_by_slug:
        return 0

    doc_state = materialize(project_root, doc_scope(slug))
    doc_classes = extract_classes_from_graph(doc_state)
    if not doc_classes:
        return 0

    aligned: list[tuple[ExtClass, URIRef]] = []
    for cls_slug, cls in doc_classes.items():
        # Only act on classes living in THIS doc's local namespace.
        if not str(cls.uri).startswith(f"{SOURCE_NS}{slug}/"):
            continue
        # Skip idempotently.
        if (cls.uri, OWL.deprecated, deprecated_lit) in doc_state:
            continue
        # Slug match against any higher-scope class.
        target = upstream_by_slug.get(cls_slug)
        if target is None or target == cls.uri:
            continue
        # Follow one hop of deprecation if the target is itself deprecated
        # (e.g., project-ext canonical was retired upward); land directly
        # at the deepest live URI.
        target = _follow_deprecation(target, ontology, deprecated_lit)
        aligned.append((cls, target))

    if not aligned:
        return 0

    added   = graph_with_ns_seed()
    removed = graph_with_ns_seed()
    for cls, target_uri in aligned:
        apply_deprecation_to_doc(
            project_root, slug, cls.uri, target_uri,
            added, removed, deprecated_lit,
        )

    seq = next_seq(project_root, doc_scope(slug))
    write_delta(
        StepDelta(
            scope     = doc_scope(slug),
            step      = "align",
            seq       = seq,
            added     = added,
            removed   = removed,
            parent_seq= seq - 1,
            agent     = agent,
            timestamp = timestamp,
        ),
        delta_path(project_root, doc_scope(slug), seq),
    )
    if console:
        for cls, target in aligned:
            console.print(f"    [dim]aligned {cls.slug} → {target}[/dim]")
    return len(aligned)


def _project_union_view(project_root: Path) -> Graph | None:
    """Build the loader's union view (foundationals + all scopes). Returns
    None when the project isn't initialised (test fixtures with bare
    deltas; alignment is then skipped as best-effort, like the
    consolidate retire pass)."""
    try:
        from src.extract_part14.loader import build_dataset, union_view
        return union_view(build_dataset(project_root))
    except FileNotFoundError:
        return None


def _index_higher_scope_classes(ontology: Graph) -> dict[str, URIRef]:
    """Index every owl:Class outside doc-local source namespaces by its
    local-name slug. Includes project-ext (canonical for cross-doc
    convergence), foundationals (LIS-14, dg, prov, dcterms), and any
    registered upstream RDL.

    When multiple classes share a slug across scopes (rare), the project
    ext URI wins — that's the closest one to the doc and the most likely
    intended target.
    """
    PROJECT_EXT_PREFIX = str(EXT)
    SOURCE_PREFIX      = str(SOURCE_NS)
    project_ext: dict[str, URIRef] = {}
    upstream:    dict[str, URIRef] = {}
    for cls_uri in ontology.subjects(RDF.type, OWL.Class):
        if not isinstance(cls_uri, URIRef):
            continue
        s = str(cls_uri)
        if s.startswith(SOURCE_PREFIX):
            continue
        local = s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if not local:
            continue
        if s.startswith(PROJECT_EXT_PREFIX):
            project_ext[local] = cls_uri
        else:
            upstream.setdefault(local, cls_uri)
    # project-ext wins on slug collision with upstream; both feed the
    # alignment map. The follow_deprecation pass collapses chains.
    out = dict(upstream)
    out.update(project_ext)
    return out


def _follow_deprecation(uri: URIRef, ontology: Graph,
                        deprecated_lit: Literal) -> URIRef:
    """If *uri* is owl:deprecated AND has a dcterms:isReplacedBy pointer,
    return the replacement URI instead. One hop only — chains beyond
    that are unusual; consolidate's retire pass collapses them anyway."""
    if (uri, OWL.deprecated, deprecated_lit) not in ontology:
        return uri
    replacement = next(
        (o for o in ontology.objects(uri, DCTERMS.isReplacedBy)
         if isinstance(o, URIRef)), None)
    return replacement or uri
