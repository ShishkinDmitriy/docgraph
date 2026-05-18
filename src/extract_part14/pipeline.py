"""Top-level entry point for the part14 add pipeline.

Validates the input PDF and hands off to the task DAG in
``pipeline_tasks.py`` (recognize → convert → extract → templates →
align → register → diagram → add). Each phase is a standalone
``@add_registry.task`` with its own ``@dirty`` predicate. The
recognize task owns identity resolution: it computes the source
hash, looks up an existing slug in sources.ttl (or mints a fresh
one), and populates ctx for all downstream tasks.

See docs/architecture/rdl-scopes.md for the operation model.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console

from src.models import ModelConfig
from src.sources import IngestError

logger = logging.getLogger(__name__)


def extract_pdf_part14(
    source: Path,
    project_root: Path,
    console: Console,
    *,
    client,
    model: ModelConfig,
    note: str | None = None,
    target: str = "add",
    exclude: "list[str] | tuple[str, ...]" = (),
    force:   "list[str] | tuple[str, ...]" = (),
) -> str:
    """Entry point for the PDF add pipeline. Returns the doc's slug.

    *target*  — which task in the add registry to run; downstream tasks
                its deps cover are pulled in too. Default "add" runs
                everything (recognize → convert → extract → templates
                → align → register → diagram). Use a partial target
                (e.g. "convert") to stop early — useful for `dg convert`
                CLI semantics.
    *exclude* — task names to skip (Gradle-style ``-x``).
    *force*   — task names whose dirty check is overridden to True so
                they run regardless. ``--force convert`` also drops
                cached HTML and re-runs the PDF→HTML LLM call.

    Idempotent — re-running on an unchanged file is a safe no-op: the
    hash matches an existing slug, the task DAG re-enters under that
    slug, every dirty check returns False, and no deltas are written.
    """
    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"{source} is not a file")
    if source.suffix.lower() != ".pdf":
        raise IngestError(f"{source.suffix} is not a PDF")

    # Hand off to the task DAG. The identity init task owns slug
    # resolution — it computes the source hash, looks up an existing
    # slug in sources.ttl (or mints a fresh one), and populates ctx
    # with slug/URIs/doc-dir for all downstream tasks.
    from src.tasks import add_registry
    forced_tasks = set(force)
    ctx = {
        "project_root":  project_root,
        "source":        source,
        "client":        client,
        "model":         model,
        "console":       console,
        "note":          note,
        "forced_tasks":  forced_tasks,
    }
    add_registry.run(target, ctx, console=console,
                     exclude=exclude, force=forced_tasks)
    return ctx["slug"]

