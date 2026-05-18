"""templates — SPARQL recognition + LLM-confirm of partial matches.

Folds lowered Part 2 / LIS-14 patterns (e.g. ScalarQuantityDatum +
UoM + value) into their lifted invocation form (tpl:Invocation).
Mechanical recognition via SPARQL; the LLM is consulted only for
partial matches where one required slot is missing.

Dirty check: clean iff there's any extract delta AND templates'
latest seq ≥ extract's latest seq. Re-fires when extract advances.

Independence: doesn't require ctx["extracted"]. The entity list is
only used for partial-match LLM confirmation; mechanical SPARQL
recognition runs regardless. Empty list is an acceptable
degradation when extract didn't run this invocation.
"""

from __future__ import annotations

from src.deltas import (
    StepDelta,
    delta_from_diff,
    delta_path,
    doc_scope,
    next_seq,
    snapshot,
    write_delta,
)
from src.extract_part14.loader import build_dataset, union_view
from src.tasks._helpers import (
    doc_state,
    has_delta_with_step,
    is_stale_wrt,
    now,
    print_delta_summary,
)
from src.tasks._registry import add_registry
from src.extract_part14.template_recognizer import fold_templates_in_place


@add_registry.task("templates", deps=("extract",))
def templates(ctx) -> None:
    # ontology is set by extract when it runs; rebuild if firing
    # without extract.
    if "ontology" not in ctx:
        ctx["ontology"] = union_view(build_dataset(ctx["project_root"]))
    extracted = ctx.get("extracted") or []

    console = ctx["console"]
    g = doc_state(ctx)
    g_before = snapshot(g)
    fold_templates_in_place(
        g, extracted=extracted, ontology=ctx["ontology"],
        base_ns=ctx["base_ns"], markdown=ctx["full_markdown"],
        client=ctx["client"], model=ctx["model"], console=console,
    )
    seq = next_seq(ctx["project_root"], doc_scope(ctx["slug"]))
    td = delta_from_diff(
        g_before, g,
        scope=doc_scope(ctx["slug"]), step="templates", seq=seq,
        parent_seq=seq - 1, agent=ctx["agent_uri"], timestamp=now(),
    )
    if len(td.added) > 0 or len(td.removed) > 0:
        write_delta(td, delta_path(ctx["project_root"], doc_scope(ctx["slug"]), seq))
        print_delta_summary(console, seq, len(td.added), len(td.removed))


@add_registry.dirty("templates")
def templates_dirty(ctx) -> bool:
    if not has_delta_with_step(ctx, "extract"):
        return False
    return (not has_delta_with_step(ctx, "templates")
            or is_stale_wrt(ctx, "templates", ("extract",)))
