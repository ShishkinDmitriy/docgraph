"""history — list the version history of a doc's graph deltas.

For every delta file in the doc's scope, prints: seq, step, +N/-M
triple counts, agent (if recorded), timestamp.
"""

from __future__ import annotations

from src.deltas import doc_scope, list_deltas_for_scope, read_delta
from src.tasks._registry import docgraph


@docgraph.task("history", deps=("resolve_slug",))
def history(ctx) -> None:
    project_root = ctx["project_root"]
    slug         = ctx["slug"]
    console      = ctx["console"]

    paths = list_deltas_for_scope(project_root, doc_scope(slug))
    if not paths:
        from src.deltas import scope_dir
        sd = scope_dir(project_root, doc_scope(slug))
        console.print(f"[yellow]No delta files for[/yellow] [bold]{slug}[/bold].")
        console.print(f"  (Looked under {sd.relative_to(project_root)}/delta.NNN.trig)")
        return

    console.print(f"\n[bold]History[/bold]  {slug}\n")
    for path in paths:
        try:
            delta = read_delta(path)
        except ValueError as exc:
            console.print(f"  [red]seq=? — {path.name}: {exc}[/red]")
            continue
        added_n   = len(delta.added)
        removed_n = len(delta.removed)
        added_str   = f"[green]+{added_n}[/green]"  if added_n   else "[dim]+0[/dim]"
        removed_str = f"[red]-{removed_n}[/red]"    if removed_n else "[dim]-0[/dim]"
        agent_str   = (f"  [dim]agent: {delta.agent}[/dim]" if delta.agent else "")
        ts_str      = (f"  [dim]{delta.timestamp.isoformat()}[/dim]"
                       if delta.timestamp else "")
        console.print(f"  [bold]seq {delta.seq:>3}[/bold]  "
                      f"{delta.step:<12} {added_str:>15} {removed_str:>15}"
                      f"{ts_str}{agent_str}")
    console.print()
