"""Tests for src/tasks.py — the task-DAG framework."""

from __future__ import annotations

import pytest

from src.tasks import FixpointError, Registry


# ── topological order ───────────────────────────────────────────────────


def test_run_executes_dependencies_in_topological_order():
    reg = Registry()
    log: list[str] = []

    @reg.task("a")
    def a(ctx): log.append("a")

    @reg.task("b", deps=("a",))
    def b(ctx): log.append("b")

    @reg.task("c", deps=("b", "a"))
    def c(ctx): log.append("c")

    reg.run("c", {})
    assert log == ["a", "b", "c"]


def test_run_walks_diamond_dependencies_without_repeating():
    """If two tasks depend on a shared upstream, the upstream still
    runs only once per fixpoint iteration."""
    reg = Registry()
    runs = {"shared": 0, "left": 0, "right": 0, "join": 0}

    @reg.task("shared")
    def shared(ctx): runs["shared"] += 1

    @reg.task("left", deps=("shared",))
    def left(ctx): runs["left"] += 1

    @reg.task("right", deps=("shared",))
    def right(ctx): runs["right"] += 1

    @reg.task("join", deps=("left", "right"))
    def join(ctx): runs["join"] += 1

    reg.run("join", {})
    assert runs == {"shared": 1, "left": 1, "right": 1, "join": 1}


# ── dirty checks ────────────────────────────────────────────────────────


def test_run_skips_tasks_whose_dirty_check_returns_false():
    reg = Registry()
    runs = {"a": 0, "b": 0}

    @reg.task("a")
    def a(ctx): runs["a"] += 1
    @reg.dirty("a")
    def a_dirty(ctx): return False    # never dirty → never runs

    @reg.task("b", deps=("a",))
    def b(ctx): runs["b"] += 1

    reg.run("b", {})
    assert runs == {"a": 0, "b": 1}


def test_iterate_true_runs_until_dirty_check_returns_false():
    """iterate=True opts a task into fixpoint re-iteration — it runs as
    long as its dirty check signals work, stopping when the predicate
    flips. Tasks without iterate=True only run once per `run()` call."""
    reg = Registry()
    state = {"counter": 0}
    invocations = {"task": 0}

    @reg.task("step", iterate=True)
    def step(ctx):
        invocations["task"] += 1
        state["counter"] += 1
    @reg.dirty("step")
    def step_dirty(ctx): return state["counter"] < 3

    reg.run("step", {})
    assert invocations["task"] == 3
    assert state["counter"] == 3


def test_default_task_runs_at_most_once_even_when_still_dirty():
    """Without iterate=True, a task runs once per `run()` call even if
    its dirty check still returns True after the run. Internal
    iteration belongs in the task body, not the framework. Protects
    against the runaway-loop class of bugs where a no-op task can't
    reduce its dirty signal."""
    reg = Registry()
    invocations = {"task": 0}

    @reg.task("step")            # NO iterate=True
    def step(ctx): invocations["task"] += 1
    @reg.dirty("step")
    def step_dirty(ctx): return True   # always dirty, but won't loop

    reg.run("step", {})
    assert invocations["task"] == 1


def test_iterate_true_raises_fixpoint_error_when_never_clean():
    reg = Registry()

    @reg.task("loop", iterate=True)
    def loop(ctx): pass
    @reg.dirty("loop")
    def loop_dirty(ctx): return True   # always dirty + iterate=True → caught

    with pytest.raises(FixpointError, match="fixpoint"):
        reg.run("loop", {}, max_iters=3)


def test_iterate_true_handles_downstream_dirty_after_upstream_runs():
    """An iterate=True upstream task's run may push state that re-dirties
    a downstream in the same fixpoint loop. The runner picks it up the
    next pass."""
    reg = Registry()
    state = {"upstream_count": 0, "downstream_done": False}

    @reg.task("upstream", iterate=True)
    def upstream(ctx): state["upstream_count"] += 1
    @reg.dirty("upstream")
    def upstream_dirty(ctx): return state["upstream_count"] < 2

    @reg.task("downstream", deps=("upstream",))
    def downstream(ctx): state["downstream_done"] = True
    @reg.dirty("downstream")
    def downstream_dirty(ctx):
        return state["upstream_count"] >= 2 and not state["downstream_done"]

    reg.run("downstream", {})
    assert state["upstream_count"] == 2 and state["downstream_done"]


# ── composite (no dirty) tasks ──────────────────────────────────────────


def test_task_without_dirty_runs_exactly_once_per_invocation():
    """Composite tasks (just deps, no real work) and one-shot tasks both
    use the no-dirty-fn shorthand. They should run once per run() call,
    not once per fixpoint iteration."""
    reg = Registry()
    invocations = {"oneshot": 0, "loop": 0}
    state = {"loop_done": False}

    @reg.task("oneshot")
    def oneshot(ctx): invocations["oneshot"] += 1

    @reg.task("loop", deps=("oneshot",))
    def loop(ctx):
        invocations["loop"] += 1
        state["loop_done"] = True
    @reg.dirty("loop")
    def loop_dirty(ctx): return not state["loop_done"]

    reg.run("loop", {})
    # oneshot ran once, even though the fixpoint loop iterated.
    assert invocations["oneshot"] == 1


# ── error paths ─────────────────────────────────────────────────────────


def test_cycle_in_deps_raises():
    reg = Registry()

    @reg.task("a", deps=("b",))
    def a(ctx): pass
    @reg.task("b", deps=("a",))
    def b(ctx): pass

    with pytest.raises(ValueError, match="cycle"):
        reg.run("a", {})


def test_unregistered_dep_raises():
    reg = Registry()

    @reg.task("a", deps=("nonexistent",))
    def a(ctx): pass

    with pytest.raises(ValueError, match="not registered"):
        reg.run("a", {})


def test_dirty_for_unknown_task_raises():
    reg = Registry()
    with pytest.raises(ValueError, match="not registered"):
        @reg.dirty("nonexistent")
        def f(ctx): return True


def test_duplicate_task_registration_raises():
    reg = Registry()

    @reg.task("a")
    def a(ctx): pass

    with pytest.raises(ValueError, match="already registered"):
        @reg.task("a")
        def a2(ctx): pass


def test_run_unknown_task_raises():
    reg = Registry()
    with pytest.raises(ValueError, match="not registered"):
        reg.run("ghost", {})


# ── skip logging ────────────────────────────────────────────────────────


class _RecordingConsole:
    def __init__(self): self.lines: list[str] = []
    def print(self, msg): self.lines.append(msg)


def test_run_logs_skipped_tasks_and_headers_when_console_provided():
    """When a console is given, the runner logs lifecycle:
    - dirty-skip → "<name>  (clean — skipped)" (once per run() call).
    - running (any task that fires, including init/composite) → "<name>".
    Task bodies log their own work below the header; the framework
    only owns lifecycle lines."""
    reg = Registry()

    @reg.task("alpha")
    def alpha(ctx): pass
    @reg.dirty("alpha")
    def alpha_dirty(ctx): return False    # never dirty → skip

    @reg.task("beta", deps=("alpha",))
    def beta(ctx): pass                   # no dirty fn → runs once

    @reg.task("gamma", deps=("beta",))
    def gamma(ctx): pass
    @reg.dirty("gamma")
    def gamma_dirty(ctx): return False    # skip

    console = _RecordingConsole()
    reg.run("gamma", {}, console=console)

    # alpha skip + beta header + gamma skip = 3 lines.
    assert len(console.lines) == 3
    assert any("alpha" in l and "skipped" in l for l in console.lines)
    assert any(l.endswith("[/bold]") and "beta"  in l for l in console.lines)
    assert any("gamma" in l and "skipped" in l for l in console.lines)


def test_skip_log_emits_once_per_task_not_per_iteration():
    """A task that stays clean while the fixpoint loop iterates should
    only print its skip line once, not every iteration."""
    reg = Registry()
    state = {"upstream_count": 0}

    @reg.task("upstream")
    def upstream(ctx):
        state["upstream_count"] += 1
    @reg.dirty("upstream")
    def upstream_dirty(ctx):
        # Stays dirty for 3 iterations, then clean — forces multi-pass.
        return state["upstream_count"] < 3

    @reg.task("downstream", deps=("upstream",))
    def downstream(ctx): pass
    @reg.dirty("downstream")
    def downstream_dirty(ctx): return False   # always clean

    console = _RecordingConsole()
    reg.run("downstream", {}, console=console)
    downstream_skip_lines = [l for l in console.lines if "downstream" in l]
    assert len(downstream_skip_lines) == 1


def test_no_console_means_no_skip_logging():
    reg = Registry()

    @reg.task("a")
    def a(ctx): pass
    @reg.dirty("a")
    def a_dirty(ctx): return False

    # Doesn't crash, doesn't print anywhere (no console).
    reg.run("a", {})


# ── exclude / force CLI plumbing ────────────────────────────────────────


def test_exclude_skips_the_named_task():
    reg = Registry()
    runs = {"a": 0, "b": 0, "c": 0}

    @reg.task("a")
    def a(ctx): runs["a"] += 1
    @reg.task("b", deps=("a",))
    def b(ctx): runs["b"] += 1
    @reg.task("c", deps=("b",))
    def c(ctx): runs["c"] += 1

    reg.run("c", {}, exclude=("b",))
    # a still runs (no exclude); b skipped; c still runs (composite
    # downstream — if it needed b's output it would self-skip via its
    # own dirty check, but here it has no dirty check).
    assert runs["a"] == 1
    assert runs["b"] == 0
    assert runs["c"] == 1


def test_force_overrides_dirty_check():
    reg = Registry()
    runs = {"a": 0}

    @reg.task("a")
    def a(ctx): runs["a"] += 1
    @reg.dirty("a")
    def a_dirty(ctx): return False    # would normally skip

    reg.run("a", {}, force=("a",))
    assert runs["a"] == 1


def test_exclude_unknown_task_raises():
    reg = Registry()
    @reg.task("a")
    def a(ctx): pass

    with pytest.raises(ValueError, match="not registered"):
        reg.run("a", {}, exclude=("ghost",))


def test_force_unknown_task_raises():
    reg = Registry()
    @reg.task("a")
    def a(ctx): pass

    with pytest.raises(ValueError, match="not registered"):
        reg.run("a", {}, force=("ghost",))
