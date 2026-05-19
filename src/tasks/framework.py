"""Task-DAG framework with dirty-driven scheduling.

Each pipeline step is a function. The function declares its name, the
names of upstream tasks it depends on, and (optionally) a `dirty`
predicate that says whether there's work left to do. The runner walks
the DAG in topological order, skipping clean tasks.

API:

    from src.tasks import Registry

    reg = Registry()

    @reg.task                              # task name defaults to fn.__name__
    def recognize(ctx): ...
    @reg.dirty                             # task name from fn.__name__ minus `_dirty`
    def recognize_dirty(ctx) -> bool: ...

    @reg.task(deps=("recognize",))
    def convert(ctx): ...
    @reg.dirty
    def convert_dirty(ctx) -> bool: ...

    reg.run("convert", ctx)   # ensures recognize is current, then convert

The docgraph add pipeline (`src/tasks/__init__.py` + the per-task
modules alongside it) is a worked example.

Design choices:

- **Dirty-driven, not timestamp-driven.** A task's `dirty(ctx)` answers
  "is there work to do?" — typically a SPARQL ASK against the current
  graph state. Cross-task ripple is natural: one task retypes some
  instances → another's dirty check matches new patterns → that one
  fires, etc.

- **Tasks without `dirty` run exactly once per `run()` call.** Init
  tasks that populate ctx (identity, load_html in the add pipeline)
  and composite targets (a no-body task that just depends on the
  chain) use this.

- **Tasks default to iterate=False.** Each runs at most once per
  `run()` invocation regardless of dirty state — internal iteration
  belongs in the task body. Opt in to fixpoint re-iteration with
  `iterate=True`. Termination guard: `FixpointError` after
  `max_iters` catches non-monotonic bugs early.

- **State is the `ctx` dict.** Tasks read what they need; what they
  share with each other goes through the graph (materialize / write
  deltas), NOT through the ctx. Independence is enforced by convention
  — no schema, no ceremony.
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


_DIRTY_SUFFIX = "_dirty"


def _strip_dirty_suffix(name: str) -> str:
    """Trim a trailing `_dirty` so `@docgraph.dirty` can derive the task
    name from the function name (`init_dirty` → task `init`)."""
    return name[:-len(_DIRTY_SUFFIX)] if name.endswith(_DIRTY_SUFFIX) else name


@dataclass
class Task:
    name:     str
    fn:       TaskFn
    deps:     tuple[str, ...] = ()
    dirty_fn: DirtyFn | None  = None
    iterate:  bool            = False


class Registry:
    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}

    # ── decorators ────────────────────────────────────────────────────

    def task(self, name_or_fn=None, *, deps: tuple[str, ...] = (),
             iterate: bool = False):
        """Register a function as a task. Three calling styles:

            @docgraph.task                              # name = fn.__name__
            @docgraph.task(deps=("identity",))          # name = fn.__name__
            @docgraph.task("init", deps=("identity",))  # explicit override

        *iterate=True* opts a task into the fixpoint loop — it can be
        re-run within a single `run()` invocation if it's still dirty
        after a prior run (template-folding-style internal iteration
        when the work itself produces more work). Default is False:
        each task runs at most once per `run()` call. The default
        keeps tasks that run-but-don't-reduce-dirty (e.g. a no-op
        align pass on a doc with nothing to align) from looping
        forever — internal iteration belongs in the task body, not
        in the framework, unless you explicitly opt in.
        """
        # @docgraph.task — bare (first arg is the function itself).
        if callable(name_or_fn):
            return self._register_task(name_or_fn.__name__, name_or_fn,
                                       deps=(), iterate=False)
        explicit_name = name_or_fn                          # str | None

        def deco(fn: TaskFn) -> TaskFn:
            return self._register_task(explicit_name or fn.__name__, fn,
                                       deps=deps, iterate=iterate)
        return deco

    def dirty(self, name_or_fn=None):
        """Register a dirty-check function. Three calling styles:

            @docgraph.dirty                  # task name = fn.__name__ minus `_dirty`
            @docgraph.dirty()                # same
            @docgraph.dirty("init")          # explicit override

        The `_dirty` suffix convention keeps the function name
        searchable while the registry uses the bare task name.
        """
        if callable(name_or_fn):
            fn = name_or_fn
            return self._register_dirty(_strip_dirty_suffix(fn.__name__), fn)
        explicit_name = name_or_fn                          # str | None

        def deco(fn: DirtyFn) -> DirtyFn:
            name = explicit_name or _strip_dirty_suffix(fn.__name__)
            return self._register_dirty(name, fn)
        return deco

    # ── internal: shared registration ─────────────────────────────────

    def _register_task(self, name: str, fn: TaskFn, *,
                        deps: tuple[str, ...], iterate: bool) -> TaskFn:
        if name in self.tasks:
            raise ValueError(f"task {name!r} already registered")
        self.tasks[name] = Task(name=name, fn=fn, deps=tuple(deps),
                                iterate=iterate)
        return fn

    def _register_dirty(self, task_name: str, fn: DirtyFn) -> DirtyFn:
        if task_name not in self.tasks:
            raise ValueError(f"task {task_name!r} not registered; "
                             f"decorate @task before @dirty")
        self.tasks[task_name].dirty_fn = fn
        return fn

    # ── runner ────────────────────────────────────────────────────────

    def run(self, target: str, ctx: Ctx, *, max_iters: int = 20,
            console=None,
            exclude: "set[str] | tuple[str, ...] | list[str]" = (),
            force:   "set[str] | tuple[str, ...] | list[str]" = ()) -> None:
        """Walk dependencies of *target* in topological order, executing
        each task that's currently dirty (or has no dirty check and
        hasn't run yet this invocation).

        *exclude* — names of tasks to skip entirely (Gradle-style ``-x``).
                    Removed from the topological order; downstream tasks
                    that needed the excluded one will discover this via
                    their dirty checks (typically by seeing absent outputs
                    and skipping themselves).
        *force*   — names of tasks whose dirty check is overridden to
                    return True (Gradle's per-task ``--rerun-task``).
                    The forced task runs once regardless of its dirty
                    state. Downstream tasks aren't auto-forced — pass
                    multiple names to force a chain.

        When *console* is provided, prints a one-line note the first
        time a task with an explicit dirty predicate is skipped because
        it's already clean. Tasks without dirty_fn (composites,
        one-shots) skip silently."""
        if target not in self.tasks:
            raise ValueError(f"task {target!r} not registered")
        exclude_set = set(exclude)
        force_set   = set(force)
        for n in exclude_set | force_set:
            if n not in self.tasks:
                raise ValueError(f"task {n!r} not registered")
        order = [n for n in self._toposort(target) if n not in exclude_set]
        ran_once:     set[str] = set()
        skip_printed: set[str] = set()
        for _ in range(max_iters):
            any_ran = False
            for name in order:
                t = self.tasks[name]
                # Default: each task runs at most once per run() call.
                # Tasks with iterate=True opt into fixpoint re-iteration.
                if name in ran_once and not t.iterate:
                    continue
                if name in force_set:
                    pass                          # forced → treat as dirty
                elif t.dirty_fn is not None and not t.dirty_fn(ctx):
                    if console is not None and name not in skip_printed:
                        console.print(
                            f"[dim]{name}  (clean — skipped)[/dim]")
                        skip_printed.add(name)
                    continue
                # Lifecycle log — print the header from the framework so
                # task bodies only log their own work, not "I'm starting".
                if console is not None:
                    console.print(f"[bold]{name}[/bold]")
                t.fn(ctx)
                ran_once.add(name)
                any_ran = True
            if not any_ran:
                return
        raise FixpointError(
            f"task {target!r} did not reach a fixpoint after "
            f"{max_iters} iterations — only iterate=True tasks may "
            f"re-run; check that each such task strictly reduces its "
            f"dirty signal"
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


