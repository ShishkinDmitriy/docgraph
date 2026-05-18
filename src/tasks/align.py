"""align — deprecate doc-local ext classes onto higher-scope canonicals.

Scope-walking instance retyping per docs/architecture/rdl-scopes.md.
For each doc-local class proposed by the LLM, find the highest-scope
class with the same slug (project ext: → upstream LIS-14 / dg). If
found, mark the doc-local URI owl:deprecated + owl:equivalentClass +
dcterms:isReplacedBy onto the canonical, and retype instances.

Dirty check: clean iff there's any extract delta AND align's latest
seq ≥ max(extract, templates) seq. Re-fires when either upstream
advances. align_doc itself is internally idempotent (already-
deprecated classes are skipped) so a redundant fire is harmless.
"""

from __future__ import annotations

from src.extract_part14.align import align_doc
from src.tasks._helpers import (
    has_delta_with_step,
    is_stale_wrt,
    now,
)
from src.tasks._registry import add_registry


@add_registry.task("align", deps=("templates",))
def align(ctx) -> None:
    # align_doc reads the materialized graph directly — no extracted
    # entity list needed.
    aligned = align_doc(
        ctx["project_root"], ctx["slug"], ontology=ctx.get("ontology"),
        agent=ctx["agent_uri"], timestamp=now(), console=ctx["console"],
    )
    if aligned:
        ctx["console"].print(f"  [dim]aligned {aligned} class(es)[/dim]")


@add_registry.dirty("align")
def align_dirty(ctx) -> bool:
    if not has_delta_with_step(ctx, "extract"):
        return False
    return (not has_delta_with_step(ctx, "align")
            or is_stale_wrt(ctx, "align", ("extract", "templates")))
