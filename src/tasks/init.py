"""init — create the `.docgraph/` project directory.

No deps (it CREATES the project root). Dirty iff `.docgraph/`
doesn't already exist under `ctx["path"]`. Forcing this task (via
`dg init --force` or `-f init`) bypasses the dirty check and
reinitialises — removing the existing `.docgraph/` before recreating.

Owns the project-init machinery: layout, the templates it stamps into
config.ttl and templates.ttl, and `init_project()` itself (still
exported as a public function for tests and other callers that want to
spin up a project without invoking the task runner).

ctx contract:
    path    — directory to initialise (must exist and be a dir)
    console — rich console for user-facing output
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from rich.console import Console

from src.project import (
    CACHE_SUBDIR,
    CONFIG_FILENAME,
    DOCGRAPH_DIR,
    DOCS_SUBDIR,
    SOURCES_FILENAME,
)
from src.sources import SOURCES_TTL_HEADER
from src.tasks._registry import docgraph


# Minimal per-project header. No copies of foundational ontologies —
# the loader reads them from vendor/ontologies/ at startup.
# See ARCHITECTURE.md § Storage layout.
_CONFIG_TTL = """\
@prefix dg:  <urn:docgraph:vocab:meta#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<> a dg:DocgraphProject ;
    dg:createdAt  "{created_at}"^^xsd:date ;
    dg:version    "0.1.0" .
"""

_TEMPLATES_REGISTRY_TTL = """\
@prefix dg:  <urn:docgraph:vocab:meta#> .

# Registry of user-authored templates loaded by this project.
# Each entry: a dg:TemplateRegistration with dg:templatePath pointing at a TTL
# file in the project repo (typically under data/templates/<custom>/).
# Bundled templates (data/templates/iso14/, data/templates/bridges/) and the
# core tpl: vocabulary are not registered here — the loader picks them up
# automatically.
"""


def init_project(
    target: Path,
    console: Console,
    *,
    force: bool = False,
) -> None:
    """Create the ``.docgraph/`` directory inside *target*.

    Raises ``FileExistsError`` if ``.docgraph/`` already exists and *force* is False.
    """
    dg_dir   = target / DOCGRAPH_DIR
    docs_dir = dg_dir / DOCS_SUBDIR
    c_dir    = dg_dir / CACHE_SUBDIR

    if dg_dir.exists() and not force:
        raise FileExistsError(f"{dg_dir} already exists. Use --force to reinitialise.")
    if dg_dir.exists() and force:
        shutil.rmtree(dg_dir)

    dg_dir.mkdir(parents=True)
    docs_dir.mkdir()
    c_dir.mkdir()
    console.print(f"  created [dim]{dg_dir}[/dim]")

    (dg_dir / CONFIG_FILENAME).write_text(
        _CONFIG_TTL.format(created_at=date.today().isoformat())
    )
    console.print(f"  wrote   [dim]{CONFIG_FILENAME}[/dim]")
    (dg_dir / "templates.ttl").write_text(_TEMPLATES_REGISTRY_TTL)
    console.print(f"  wrote   [dim]templates.ttl[/dim]")

    (dg_dir / SOURCES_FILENAME).write_text(SOURCES_TTL_HEADER)
    console.print(f"  wrote   [dim]{SOURCES_FILENAME}[/dim]")

    console.print(
        f"\n[green]Initialised docgraph project in[/green] [bold]{target}[/bold]\n"
        f"Add a source with [dim]docgraph add <file>[/dim]."
    )


def _target_dir(ctx) -> Path:
    """CLI args[0] (default cwd) — the directory to initialise."""
    args = ctx.get("args", ())
    return Path(args[0] if args else ".").resolve()


@docgraph.task
def init(ctx) -> None:
    path = _target_dir(ctx)
    if not path.is_dir():
        raise NotADirectoryError(f"{path} is not a directory")
    init_project(
        path, ctx["console"],
        force="init" in ctx.get("forced_tasks", set()),
    )


@docgraph.dirty
def init_dirty(ctx) -> bool:
    return not (_target_dir(ctx) / DOCGRAPH_DIR).exists()
