"""Task-DAG framework + the per-doc add pipeline.

The framework lives in `framework.py` (Registry, Task, FixpointError,
Ctx, TaskFn, DirtyFn). It's pipeline-agnostic — any docgraph workflow
that wants dirty-driven scheduling instantiates a `Registry()` and
decorates functions against it.

The add pipeline (one task per file in this package, names match the
task names) drives PDF ingestion:

  identity → recognize → convert → load_html → extract → templates
        → align → register → snapshot → diagram → add

`docgraph` (defined in `_registry.py` to avoid circular imports
between this `__init__` and the per-task modules) holds them. The
imports below trigger each task's `@docgraph.task(…)` /
`@docgraph.dirty(…)` decorators so the registry is fully
populated when external callers reach this point.

External callers:

    from src.tasks import Registry             # build a custom pipeline
    from src.tasks import docgraph         # use the add pipeline
    docgraph.run("add", ctx, console=...)

Shared task helpers (delta inspection, per-step logging) live in
`_helpers.py`. Anything used by only one task lives in that task's
module.
"""

from __future__ import annotations

from src.tasks.framework import (
    Ctx,
    DirtyFn,
    FixpointError,
    Registry,
    Task,
    TaskFn,
)
from src.tasks._registry import docgraph

# Trigger task registration by importing each task module. Order
# doesn't matter for correctness (the framework toposorts from
# deps); the grouping below is purely for readability.
#
# Project lifecycle.
from src.tasks import init          # noqa: F401
from src.tasks import clean         # noqa: F401
# Per-doc add pipeline (in topological order).
from src.tasks import identity      # noqa: F401
from src.tasks import recognize     # noqa: F401
from src.tasks import convert       # noqa: F401
from src.tasks import load_html     # noqa: F401
from src.tasks import extract       # noqa: F401
from src.tasks import templates     # noqa: F401
from src.tasks import align         # noqa: F401
from src.tasks import register      # noqa: F401
from src.tasks import snapshot      # noqa: F401
from src.tasks import diagram       # noqa: F401
from src.tasks import add           # noqa: F401

__all__ = [
    "Ctx", "DirtyFn", "FixpointError", "Registry", "Task", "TaskFn",
    "docgraph",
]
