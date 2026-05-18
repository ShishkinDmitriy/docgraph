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


def test_run_iterates_until_dirty_check_returns_false():
    """The canonical fixpoint pattern: a task runs as long as its dirty
    check signals work to do, and only stops when the predicate flips."""
    reg = Registry()
    state = {"counter": 0}
    invocations = {"task": 0}

    @reg.task("step")
    def step(ctx):
        invocations["task"] += 1
        state["counter"] += 1
    @reg.dirty("step")
    def step_dirty(ctx): return state["counter"] < 3

    reg.run("step", {})
    assert invocations["task"] == 3
    assert state["counter"] == 3


def test_run_raises_fixpoint_error_when_never_clean():
    reg = Registry()

    @reg.task("loop")
    def loop(ctx): pass
    @reg.dirty("loop")
    def loop_dirty(ctx): return True   # always dirty → infinite loop

    with pytest.raises(FixpointError, match="fixpoint"):
        reg.run("loop", {}, max_iters=3)


def test_run_handles_downstream_dirty_after_upstream_runs():
    """An upstream task's run may push state that re-dirties a downstream
    in the same fixpoint iteration. The runner picks it up next pass."""
    reg = Registry()
    state = {"upstream_done": False, "downstream_done": False}

    @reg.task("upstream")
    def upstream(ctx): state["upstream_done"] = True
    @reg.dirty("upstream")
    def upstream_dirty(ctx): return not state["upstream_done"]

    @reg.task("downstream", deps=("upstream",))
    def downstream(ctx): state["downstream_done"] = True
    @reg.dirty("downstream")
    def downstream_dirty(ctx):
        return state["upstream_done"] and not state["downstream_done"]

    reg.run("downstream", {})
    assert state["upstream_done"] and state["downstream_done"]


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
