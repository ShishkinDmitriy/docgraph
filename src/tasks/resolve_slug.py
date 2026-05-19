"""resolve_slug — populate ctx["slug"] from ctx["args"][0].

Foundational task for any per-doc CLI that addresses a doc by name
(slug) or by its original file path. args[0] is whatever the user
typed: a slug (registered in sources.ttl) or a path to the source
file (resolved by absolute path or by content hash for moved files).

No dirty check — idempotent, runs at most once per `run()` call.
Two early-exit cases:
  - "slug" already in ctx — pre-populated by an upstream task
    (e.g. identity, when running the add pipeline from a PDF).
  - args empty — nothing to resolve; the slug will be set elsewhere
    (e.g. by identity from ctx["path"]).

ctx contract:
    args         — tuple of CLI positional args; args[0] is the target
    project_root — required (set by resolve_project dep)
"""

from __future__ import annotations

from pathlib import Path

import click

from src.sources import compute_hash, list_sources
from src.tasks._registry import docgraph


@docgraph.task("resolve_slug", deps=("resolve_project",))
def resolve_slug(ctx) -> None:
    if "slug" in ctx:
        return                              # pre-populated by CLI / upstream task
    args = ctx.get("args", ())
    if not args:
        return                              # another task will set slug (e.g. identity)

    project_root = ctx["project_root"]
    target = args[0]
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
