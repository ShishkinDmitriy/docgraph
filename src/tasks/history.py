"""history — list the version history of a doc's graph deltas.

For every delta file in the doc's scope, prints: seq, step, +N/-M
triple counts, agent (if recorded), timestamp.
"""

from __future__ import annotations

from rich.table import Table

from src.deltas import doc_scope, list_deltas_for_scope, read_delta
from src.tasks._registry import docgraph
from src.tasks.resolve_slug import require_slug


@docgraph.task(desc="List the version history of a doc's deltas", quiet=True,
               deps=("resolve_slug",))
def history(ctx) -> None:
    slug         = require_slug(ctx, "history")
    project_root = ctx["project_root"]
    console      = ctx["console"]

    paths = list_deltas_for_scope(project_root, doc_scope(slug))
    if not paths:
        from src.deltas import scope_dir
        sd = scope_dir(project_root, doc_scope(slug))
        console.print(f"[yellow]No delta files for[/yellow] [bold]{slug}[/bold].")
        console.print(f"  (Looked under {sd.relative_to(project_root)}/delta.NNN.trig)")
        return

    console.print(f"\n[bold]History[/bold]  {slug}\n")
    table = Table(show_header=True, header_style="bold cyan", box=None,
                  padding=(0, 2))
    table.add_column("Seq",       justify="right")
    table.add_column("Step")
    table.add_column("Added",     justify="right")
    table.add_column("Removed",   justify="right")
    table.add_column("Timestamp", style="dim")
    table.add_column("Agent",     style="dim")
    for path in paths:
        try:
            delta = read_delta(path)
        except ValueError as exc:
            table.add_row(f"[red]?[/red]", path.name, "", "",
                          f"[red]{exc}[/red]", "")
            continue
        added_n   = len(delta.added)
        removed_n = len(delta.removed)
        table.add_row(
            str(delta.seq),
            delta.step,
            f"[green]+{added_n}[/green]"  if added_n   else "[dim]+0[/dim]",
            f"[red]-{removed_n}[/red]"    if removed_n else "[dim]-0[/dim]",
            delta.timestamp.isoformat()   if delta.timestamp else "",
            str(delta.agent)              if delta.agent     else "",
        )
    console.print(table)
    console.print()
