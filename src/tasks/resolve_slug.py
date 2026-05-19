"""resolve_slug — populate ctx["slug"] from ctx["target"].

Foundational task for any per-doc CLI that addresses a doc by name
(slug) or by its original file path. ctx["target"] is whatever the
user typed: a slug (registered in sources.ttl) or a path to the
original source file (resolved by absolute path or by content hash
for moved files).

No dirty check — idempotent, runs at most once per `run()` call.
Three early-exit cases:
  - "slug" already in ctx — pre-populated by the CLI (e.g. diagram --all
    iterates over slugs).
  - "target" absent from ctx — the slug will be populated by another
    task in this run (e.g. identity, when running the add pipeline
    from a PDF path rather than a slug).

ctx contract:
    target       — slug or path (optional; absent in the add pipeline)
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
        return                              # pre-populated by CLI
    if "target" not in ctx:
        return                              # another task will set slug (e.g. identity)

    project_root = ctx["project_root"]
    target = ctx["target"]
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
        raise click.UsageError(
            f"{p} is not registered in this project "
            f"(run `docgraph status` to list sources).")

    if target in by_slug:
        ctx["slug"] = target
        return
    raise click.UsageError(
        f"no source registered as {target!r} "
        f"(run `docgraph status` to list sources).")
