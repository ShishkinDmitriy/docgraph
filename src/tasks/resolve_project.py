"""resolve_project — populate ctx["project_root"] from ctx["path"].

Foundational task: every task that operates inside an existing
`.docgraph/` project (clean, consolidate, identity → per-doc chain)
declares this as a dep and then just reads `ctx["project_root"]`.

No dirty check — it's idempotent and runs at most once per `run()`
call. If ctx already has `project_root` (some CLI commands pre-populate
it directly, e.g. the standalone diagram path), the body returns
without touching `ctx["path"]`, so those commands don't need to
include a path key.

ctx contract:
    path    — required (unless project_root already in ctx)
    console — required for the "Project root: …" announcement
"""

from __future__ import annotations

from pathlib import Path

from src.project import find_project_root
from src.sources import IngestError
from src.tasks._registry import docgraph


@docgraph.task("resolve_project")
def resolve_project(ctx) -> None:
    if "project_root" in ctx:
        return                              # pre-populated by CLI (e.g. diagram standalone)

    path = ctx["path"].resolve()
    ctx["path"] = path
    start = path if path.is_dir() else path.parent
    project_root = find_project_root(start) or find_project_root(Path.cwd())
    if project_root is None:
        raise IngestError("not a docgraph project (run `docgraph init`)")
    ctx["project_root"] = project_root
    ctx["console"].print(f"Project root: [dim]{project_root}[/dim]")
