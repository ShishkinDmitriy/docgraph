"""init — create the `.docgraph/` project directory.

No deps (it CREATES the project root). Dirty iff `.docgraph/`
doesn't already exist under `ctx["path"]`. Forcing this task (via
`dg init --force` or `-f init`) bypasses the dirty check and
reinitialises — `init_project` removes the existing `.docgraph/`
before recreating.

ctx contract:
    path    — directory to initialise (must exist and be a dir)
    console — rich console for user-facing output
"""

from __future__ import annotations

from src.project import DOCGRAPH_DIR, init_project
from src.tasks._registry import docgraph


@docgraph.task("init")
def init(ctx) -> None:
    path = ctx["path"]
    if not path.is_dir():
        raise NotADirectoryError(f"{path} is not a directory")
    init_project(
        path, ctx["console"],
        force="init" in ctx.get("forced_tasks", set()),
    )


@docgraph.dirty("init")
def init_dirty(ctx) -> bool:
    return not (ctx["path"] / DOCGRAPH_DIR).exists()
