"""consolidate — promote equivalent ext: classes to project scope.

Project-wide (not per-doc). Scans every doc-scope graph for ext-class
declarations; classes declared in ≥threshold docs are merged into a
canonical definition at the project ext: namespace, and each
contributing doc gets a `consolidate` delta that removes the doc-local
class and rewrites instance triples to the new canonical URI.

See docs/architecture/rdl-scopes.md for the operation model.

ctx contract:
    path      — directory whose enclosing `.docgraph/` is the target
    console   — rich console for user-facing output
    threshold — optional (default 2)

Dirty check: clean iff no non-promoted ext-class meets the threshold.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from src.deltas import list_scopes, materialize, project_scope
from src.extract_part14.consolidate import walk_consolidate
from src.extract_part14.ext_ontology import extract_classes_from_graph
from src.project import find_project_root
from src.sources import IngestError
from src.tasks._registry import docgraph

_DEFAULT_THRESHOLD = 2


def _resolve_project(ctx) -> Path:
    project_root = find_project_root(ctx["path"].resolve())
    if project_root is None:
        raise IngestError("not a docgraph project (run `docgraph init`)")
    return project_root


def find_consolidation_candidates(
    project_root: Path, *, threshold: int = _DEFAULT_THRESHOLD,
) -> list[tuple[str, list[str]]]:
    """Slugs that meet the threshold AND aren't already promoted to
    project scope. Returns `[(slug, [contributing_doc, ...]), ...]`.
    Used by the dirty check; also handy for ad-hoc previews."""
    project_state = materialize(project_root, project_scope())
    already_promoted = set(extract_classes_from_graph(project_state).keys())

    contributors_by_slug: dict[str, list[str]] = defaultdict(list)
    for scope in list_scopes(project_root):
        if scope.kind != "doc" or not scope.name:
            continue
        per_doc_classes = extract_classes_from_graph(
            materialize(project_root, scope))
        for slug in per_doc_classes:
            if slug in already_promoted:
                continue
            contributors_by_slug[slug].append(scope.name)

    return [(slug, contribs)
            for slug, contribs in sorted(contributors_by_slug.items())
            if len(contribs) >= threshold]


@docgraph.task("consolidate")
def consolidate(ctx) -> None:
    console = ctx["console"]
    threshold = ctx.get("threshold", _DEFAULT_THRESHOLD)
    console.print(f"  threshold ≥{threshold} docs")
    decisions = walk_consolidate(
        _resolve_project(ctx), threshold=threshold, console=console)
    if decisions:
        console.print(f"  → consolidated {len(decisions)} class(es) "
                      f"into project scope")


@docgraph.dirty("consolidate")
def consolidate_dirty(ctx) -> bool:
    return bool(find_consolidation_candidates(
        _resolve_project(ctx),
        threshold=ctx.get("threshold", _DEFAULT_THRESHOLD),
    ))
