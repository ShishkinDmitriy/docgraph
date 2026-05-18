"""The add-pipeline task registry, in its own module to avoid
circular imports between the package `__init__` and the per-task
modules that decorate against it.

Each task module does `from src.tasks._registry
import add_registry` and then `@add_registry.task(...)`. The package
`__init__` imports every task module to trigger the decorator
side-effects so `add_registry` is fully populated when external
callers import it.
"""

from __future__ import annotations

from src.tasks.framework import Registry

add_registry = Registry()
