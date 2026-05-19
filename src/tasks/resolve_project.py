"""resolve_project — populate ctx["project_root"] from ctx["args"][0] or cwd.

Foundational task: every task that operates inside an existing
`.docgraph/` project (clean, consolidate, identity → per-doc chain)
declares this as a dep and then just reads `ctx["project_root"]`.

No dirty check — idempotent, runs at most once per `run()` call. If
ctx already has `project_root`, returns early (some test setups
pre-populate it).

Argument interpretation: if ctx["args"][0] is a filesystem path that
exists, walk up from it (or its parent, if a file) to find the project.
Otherwise (no args, or args[0] is a slug-like string) walk up from cwd.
When args[0] is an existing path, it's resolved into `ctx["path"]` for
downstream tasks that need the input file (e.g. identity reads
ctx["path"] as the PDF to ingest).

ctx contract:
    args    — optional tuple of positional CLI args
    console — for the "Project root: …" announcement
"""

from __future__ import annotations

from pathlib import Path

from src.project import find_project_root
from src.sources import IngestError
from src.tasks._registry import docgraph


@docgraph.task("resolve_project")
def resolve_project(ctx) -> None:
    if "project_root" in ctx:
        return                              # pre-populated

    args = ctx.get("args", ())
    candidate = Path(args[0]).resolve() if args else None
    if candidate is not None and candidate.exists():
        ctx["path"] = candidate
        start = candidate if candidate.is_dir() else candidate.parent
    else:
        # args[0] is a slug (or absent) — search from cwd.
        start = Path.cwd()

    project_root = find_project_root(start) or find_project_root(Path.cwd())
    if project_root is None:
        raise IngestError("not a docgraph project (run `docgraph init`)")
    ctx["project_root"] = project_root
    ctx["console"].print(f"Project root: [dim]{project_root}[/dim]")
