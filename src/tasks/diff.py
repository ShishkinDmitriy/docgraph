"""diff — show triples added/removed between two seqs of a doc.

`materialize(at_seq=seq_b)` minus `materialize(at_seq=seq_a)` — i.e.
what triples got added/removed by the steps with seq in (seq_a, seq_b].
Useful to see what a particular phase actually did without grepping
individual delta files.

CLI: `docgraph diff TARGET SEQ_A SEQ_B`

ctx contract:
    project_root — required (via resolve_project)
    slug         — required (via resolve_slug)
    args         — args[1] = seq_a, args[2] = seq_b
    console      — required
"""

from __future__ import annotations

import click

from src.deltas import doc_scope, materialize
from src.tasks._registry import docgraph


@docgraph.task(desc="Show triple-level diff between two seqs of a doc", quiet=True,
               deps=("resolve_slug",))
def diff(ctx) -> None:
    args = ctx.get("args", ())
    if len(args) < 3:
        raise click.UsageError("usage: docgraph diff TARGET SEQ_A SEQ_B")
    project_root = ctx["project_root"]
    slug         = ctx["slug"]
    seq_a        = int(args[1])
    seq_b        = int(args[2])
    console      = ctx["console"]
    scope = doc_scope(slug)

    state_a = materialize(project_root, scope, at_seq=seq_a)
    state_b = materialize(project_root, scope, at_seq=seq_b)
    a_set = set(state_a)
    b_set = set(state_b)
    added   = b_set - a_set
    removed = a_set - b_set

    console.print(f"\n[bold]Diff[/bold]  {slug}  "
                  f"seq {seq_a} → {seq_b}\n")
    console.print(f"  Added:   [green]+{len(added)}[/green] triples")
    console.print(f"  Removed: [red]-{len(removed)}[/red] triples\n")

    if added:
        console.print("[bold green]+ Added[/bold green]")
        for triple in sorted(added, key=str)[:50]:
            console.print(f"  [green]+[/green]  {triple[0]}  {triple[1]}  {triple[2]}")
        if len(added) > 50:
            console.print(f"  [dim]…and {len(added) - 50} more[/dim]")
        console.print()
    if removed:
        console.print("[bold red]- Removed[/bold red]")
        for triple in sorted(removed, key=str)[:50]:
            console.print(f"  [red]-[/red]  {triple[0]}  {triple[1]}  {triple[2]}")
        if len(removed) > 50:
            console.print(f"  [dim]…and {len(removed) - 50} more[/dim]")
        console.print()
