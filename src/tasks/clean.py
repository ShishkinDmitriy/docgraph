"""clean — wipe every ingested source from a project.

Removes per-doc dirs under `.docgraph/docs/<slug>/`, legacy flat
`.docgraph/graphs/*.{ttl,trig}` files, the embeddings cache, and
resets sources.ttl to empty. Leaves config.ttl, templates.ttl, and
foundational ontologies untouched — the project itself stays
initialised; only the ingested content is gone.

ctx contract:
    project_root — required (set by resolve_project dep)
    console      — rich console for user-facing output
"""

from __future__ import annotations

import shutil
from pathlib import Path

from src.project import DOCGRAPH_DIR, DOCS_SUBDIR, graphs_dir
from src.sources import reset_sources
from src.tasks._registry import docgraph


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


@docgraph.task(desc="Wipe every ingested source from a project",
               deps=("resolve_project",))
def clean(ctx) -> None:
    project_root = ctx["project_root"]
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


@docgraph.dirty
def clean_dirty(ctx) -> bool:
    project_root = ctx["project_root"]
    if list_targets(project_root):
        return True
    from src.embeddings import EMBEDDINGS_FILENAME
    return (project_root / DOCGRAPH_DIR / EMBEDDINGS_FILENAME).is_file()
