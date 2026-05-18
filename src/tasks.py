"""Tiny task-DAG framework with dirty-driven fixpoint scheduling.

Each pipeline step is a function. The function declares its name, the
names of upstream tasks it depends on, and (optionally) a `dirty`
predicate that says whether there's work left to do. The runner walks
the DAG in topological order and re-iterates until no task is dirty.

API:

    from src import tasks

    reg = tasks.Registry()

    @reg.task("recognize")
    def recognize(ctx): ...
    @reg.dirty("recognize")
    def recognize_dirty(ctx) -> bool: ...

    @reg.task("convert", deps=("recognize",))
    def convert(ctx): ...
    @reg.dirty("convert")
    def convert_dirty(ctx) -> bool: ...

    reg.run("convert", ctx)   # ensures recognize is current, then convert

Design choices:

- **Dirty-driven, not timestamp-driven.** A task's `dirty(ctx)` answers
  "is there work to do?" — typically a SPARQL ASK against the current
  graph state. This makes cross-task ripple effects work naturally:
  consolidate retypes some instances → templates dirty check now
  matches new lowered patterns → templates fires → so on, until
  quiescence. The runner iterates until no task is dirty.

- **Tasks without `dirty` run exactly once per `run()` call.** Useful
  for composite tasks ("add" that just depends on the per-doc chain) or
  always-applicable side effects (writing the registration entry).

- **Termination requires monotonicity.** Every task's `run` must
  strictly reduce its own dirty signal — otherwise the fixpoint loop
  is infinite. The runner caps iterations to catch bugs early
  (`FixpointError` after `max_iters`).

- **State is the `ctx` dict.** Tasks read what they need; what they
  share with each other goes through the graph (materialize / write
  deltas), NOT through the ctx. Independence is enforced by convention
  — no schema, no ceremony.

Per-test isolation: instantiate `Registry()`; for production, use
the module-level `DEFAULT` registry (`tasks.task`, `tasks.dirty`,
`tasks.run` shortcuts bound to it).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Ctx = dict[str, Any]
TaskFn  = Callable[[Ctx], None]
DirtyFn = Callable[[Ctx], bool]


class FixpointError(RuntimeError):
    """Raised when a `run()` call exceeds its iteration budget — usually
    means at least one task's `run` doesn't reduce its own dirty
    signal, or two tasks chase each other's outputs forever."""


@dataclass
class Task:
    name:     str
    fn:       TaskFn
    deps:     tuple[str, ...] = ()
    dirty_fn: DirtyFn | None  = None


class Registry:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    # ── decorators ────────────────────────────────────────────────────

    def task(self, name: str, *, deps: tuple[str, ...] = ()) -> Callable[[TaskFn], TaskFn]:
        def deco(fn: TaskFn) -> TaskFn:
            if name in self.tasks:
                raise ValueError(f"task {name!r} already registered")
            self.tasks[name] = Task(name=name, fn=fn, deps=tuple(deps))
            return fn
        return deco

    def dirty(self, task_name: str) -> Callable[[DirtyFn], DirtyFn]:
        def deco(fn: DirtyFn) -> DirtyFn:
            if task_name not in self.tasks:
                raise ValueError(f"task {task_name!r} not registered; "
                                 f"decorate @task before @dirty")
            self.tasks[task_name].dirty_fn = fn
            return fn
        return deco

    # ── runner ────────────────────────────────────────────────────────

    def run(self, target: str, ctx: Ctx, *, max_iters: int = 20) -> None:
        """Walk dependencies of *target* in topological order, executing
        each task that's currently dirty (or has no dirty check and
        hasn't run yet this invocation). Re-iterate until quiescence,
        or raise FixpointError after max_iters."""
        if target not in self.tasks:
            raise ValueError(f"task {target!r} not registered")
        order = self._toposort(target)
        ran_once: set[str] = set()
        for _ in range(max_iters):
            any_ran = False
            for name in order:
                t = self.tasks[name]
                if t.dirty_fn is None:
                    if name in ran_once:
                        continue
                else:
                    if not t.dirty_fn(ctx):
                        continue
                t.fn(ctx)
                ran_once.add(name)
                any_ran = True
            if not any_ran:
                return
        raise FixpointError(
            f"task {target!r} did not reach a fixpoint after "
            f"{max_iters} iterations — check that each task's `run` "
            f"strictly reduces its own dirty signal"
        )

    # ── helpers ───────────────────────────────────────────────────────

    def _toposort(self, target: str) -> list[str]:
        order:    list[str] = []
        done:     set[str]  = set()
        visiting: set[str]  = set()

        def visit(n: str) -> None:
            if n in done:
                return
            if n in visiting:
                raise ValueError(f"task dependency cycle involving {n!r}")
            if n not in self.tasks:
                raise ValueError(f"task {n!r} not registered "
                                 f"(referenced as a dependency)")
            visiting.add(n)
            for d in self.tasks[n].deps:
                visit(d)
            visiting.discard(n)
            done.add(n)
            order.append(n)

        visit(target)
        return order


# ── Module-level default registry ────────────────────────────────────

DEFAULT = Registry()
task   = DEFAULT.task
dirty  = DEFAULT.dirty
run    = DEFAULT.run
