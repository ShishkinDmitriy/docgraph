"""Shared helpers used by multiple add-pipeline task modules.

Anything used by only one task module lives in that module itself
(e.g., identity's slug-resolution, load_html's ctx population,
recognize's hash-match check). This file is for utilities that
genuinely cross task boundaries.

Two groups:

  Per-step logging + state read:
    `now`, `print_delta_summary`, `doc_state`.

  Delta inspection used by dirty checks:
    `latest_delta_of_step` — read the latest delta of a given step
      (recognize, convert use this for content-match dirty checks).
    `has_delta_with_step`, `latest_seq_of_step` — convenience
      wrappers for "any delta exists" / "max seq" queries.
    `is_stale_wrt` — seq comparison between a task's last output and
      its upstream's (extract, templates, align use this for
      cascade-on-force).
"""

from __future__ import annotations

from datetime import datetime, timezone

from rdflib import Graph

from src.deltas import (
    doc_scope,
    list_deltas_for_scope,
    materialize,
    read_delta,
)


def now() -> datetime:
    return datetime.now(timezone.utc)


def print_delta_summary(console, seq: int, added: int, removed: int) -> None:
    """Per-step write confirmation under each pipeline phase."""
    counts = f"[green]+{added}[/green]"
    if removed:
        counts += f" [red]-{removed}[/red]"
    console.print(f"  wrote   [dim]delta.{seq:03d}.trig[/dim] ({counts})")


def doc_state(ctx) -> Graph:
    return materialize(ctx["project_root"], doc_scope(ctx["slug"]))


# ── delta-inspection helpers used by dirty checks ─────────────────────


def has_delta_with_step(ctx, step: str) -> bool:
    """True iff any delta in this doc's scope was written with step=*step*."""
    return latest_seq_of_step(ctx, step) > 0


def latest_seq_of_step(ctx, step: str) -> int:
    """Highest seq among deltas with this step in the doc scope, or 0."""
    d = latest_delta_of_step(ctx, step)
    return d.seq if d is not None else 0


def latest_delta_of_step(ctx, step: str):
    """Latest StepDelta with the given step name in the doc scope, or None.

    Used by dirty checks that need to inspect the actual content of
    their last output (e.g. recognize verifying the recorded hash
    matches the source's current hash), not just "did I ever run"."""
    latest = None
    for path in list_deltas_for_scope(ctx["project_root"],
                                       doc_scope(ctx["slug"])):
        try:
            d = read_delta(path)
        except ValueError:
            continue
        if d.step != step:
            continue
        if latest is None or d.seq > latest.seq:
            latest = d
    return latest


def is_stale_wrt(ctx, my_step: str, upstream_steps: tuple[str, ...]) -> bool:
    """True iff the latest *upstream_steps* delta has a higher seq than
    the latest *my_step* delta — i.e. upstream has been re-run since my
    last output and my output is stale. Also True if upstream has any
    delta and I have none.

    Used by extract/templates/align dirty checks so forcing an
    upstream task (e.g. `-f convert`) cascades through dirty checks
    automatically — downstream tasks notice their inputs changed."""
    my_seq = latest_seq_of_step(ctx, my_step)
    upstream_seq = max(
        (latest_seq_of_step(ctx, s) for s in upstream_steps),
        default=0,
    )
    if upstream_seq == 0:
        return False                       # nothing upstream to be stale against
    return my_seq < upstream_seq
