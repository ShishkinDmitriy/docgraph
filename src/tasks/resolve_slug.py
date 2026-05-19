"""resolve_slug — populate ctx["slug"] from ctx["args"][0].

Foundational task for any per-doc CLI that addresses a doc by name
(slug) or by its original file path. args[0] is whatever the user
typed: a slug (registered in sources.ttl) or a path to the source
file (resolved by absolute path or by content hash for moved files).

Dirty check: clean if "slug" is already in ctx (pre-populated, or set
by an upstream task like identity from a PDF) OR if there's no target
arg to resolve (the add pipeline from a PDF — identity sets slug).

ctx contract:
    args         — tuple of CLI positional args; args[0] is the target
    project_root — required (set by resolve_project dep)
"""

from __future__ import annotations

from pathlib import Path

import click

from src.sources import compute_hash, list_sources
from src.tasks._registry import docgraph


@docgraph.task(deps=("resolve_project",))
def resolve_slug(ctx) -> None:
    project_root = ctx["project_root"]
    target = ctx["args"][0]
    sources = list_sources(project_root)
    by_slug = {s["slug"]: s for s in sources}

    p = Path(target)
    if p.exists() and p.is_file():
        absolute = str(p.resolve())
        for s in sources:
            if s["sourcePath"] == absolute:
                ctx["slug"] = s["slug"]
                return
        # Path didn't match — try content hash for moved/renamed files.
        file_hash = compute_hash(p.resolve())
        for s in sources:
            if s["fileHash"] == file_hash:
                ctx["slug"] = s["slug"]
                return
        # File exists but isn't registered yet — that's fine for `dg add`.
        # Identity will mint a new slug from the file. For read-only
        # commands (history, view, …) downstream tasks will raise their
        # own "no graph for this slug" errors.
        return

    if target in by_slug:
        ctx["slug"] = target
        return
    raise click.UsageError(
        f"no source registered as {target!r} "
        f"(run `docgraph status` to list sources).")


@docgraph.dirty
def resolve_slug_dirty(ctx) -> bool:
    if "slug" in ctx:
        return False                        # pre-populated
    return bool(ctx.get("args"))            # nothing to resolve without a target
