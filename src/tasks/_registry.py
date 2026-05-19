"""The project-wide task registry.

Lives in its own module to avoid circular imports between the package
`__init__` and the per-task modules that decorate against it. Each
task module does `from src.tasks._registry import docgraph` and then
`@docgraph.task(...)`. The package `__init__` imports every task
module to trigger the decorator side-effects so the registry is
fully populated when external callers reach `from src.tasks import
docgraph`.

A single registry holds every task — add-pipeline phases today,
future consolidate/enrich phases, project-lifecycle tasks. Pipelines
are just subtrees rooted at a particular target (e.g.
`docgraph.run("add", ctx)` walks add's deps, `docgraph.run("consolidate",
ctx)` walks consolidate's). They don't collide unless they share a
task by name — task names are globally unique.
"""

from __future__ import annotations

from src.tasks.framework import Registry

docgraph = Registry()
