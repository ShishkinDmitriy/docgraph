"""clean — wipe every ingested source from a project.

Removes per-doc dirs under `.docgraph/docs/<slug>/`, legacy flat
`.docgraph/graphs/*.{ttl,trig}` files, the embeddings cache, and
resets sources.ttl to empty. Leaves config.ttl, templates.ttl, and
foundational ontologies untouched — the project itself stays
initialised; only the ingested content is gone.

The CLI prompts for confirmation before invoking this task (or pass
`-y` to skip). Once `_run_task("clean", ...)` is called, the removal
is unconditional.

ctx contract:
    path    — directory whose enclosing `.docgraph/` is the target
    console — rich console for user-facing output
"""

from __future__ import annotations

import shutil
from pathlib import Path

from src.project import (
    DOCGRAPH_DIR,
    DOCS_SUBDIR,
    find_project_root,
    graphs_dir,
    reset_sources,
)
from src.sources import IngestError
from src.tasks._registry import docgraph


def _resolve_project(ctx) -> Path:
    project_root = find_project_root(ctx["path"].resolve())
    if project_root is None:
        raise IngestError("not a docgraph project (run `docgraph init`)")
    return project_root


def list_targets(project_root: Path) -> list[Path]:
    """Files and dirs the clean task would remove (does NOT include
    sources.ttl reset or the embeddings cache — those are handled
    inline in the task body and don't fit the per-target preview).
    Public for the CLI's preview step."""
    targets: list[Path] = []
    docs_root = project_root / DOCGRAPH_DIR / DOCS_SUBDIR
    if docs_root.is_dir():
        targets.extend(sorted(p for p in docs_root.iterdir() if p.is_dir()))
    legacy = graphs_dir(project_root)
    if legacy.is_dir():
        targets.extend(sorted(p for p in legacy.iterdir()
                              if p.suffix in (".ttl", ".trig")))
    return targets


@docgraph.task("clean")
def clean(ctx) -> None:
    project_root = _resolve_project(ctx)
    console = ctx["console"]

    targets = list_targets(project_root)
    for p in targets:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
    if targets:
        console.print(f"  removed [bold]{len(targets)}[/bold] ingested graph(s)")

    reset_sources(project_root)
    console.print(f"  reset   [dim]sources.ttl[/dim]")

    from src.embeddings import EMBEDDINGS_FILENAME
    emb_path = project_root / DOCGRAPH_DIR / EMBEDDINGS_FILENAME
    if emb_path.is_file():
        emb_path.unlink()
        console.print(f"  removed [dim]{EMBEDDINGS_FILENAME}[/dim]")


@docgraph.dirty("clean")
def clean_dirty(ctx) -> bool:
    project_root = _resolve_project(ctx)
    if list_targets(project_root):
        return True
    from src.embeddings import EMBEDDINGS_FILENAME
    return (project_root / DOCGRAPH_DIR / EMBEDDINGS_FILENAME).is_file()
