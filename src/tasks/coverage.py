"""coverage — report which atomic HTML units are cited in the graph.

For each element with `id="id-N"` in the canonical HTML, check whether
any graph triple cites `<doc#id-N>` directly OR cites a `<doc#class-N>`
fragment that covers it. Prints total coverage, lists uncovered units,
and breaks down by data-note section.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from src.coverage import coverage_for_files
from src.deltas import doc_scope, materialize
from src.html_io import html_paths
from src.project import doc_dir
from src.tasks._registry import docgraph


@docgraph.task("coverage", deps=("resolve_slug",))
def coverage(ctx) -> None:
    project_root = ctx["project_root"]
    slug         = ctx["slug"]
    console      = ctx["console"]

    sd = doc_dir(project_root, slug)
    found = html_paths(sd) if sd.exists() else []
    html_path: Path | None = found[0] if found else None

    g = materialize(project_root, doc_scope(slug))
    if html_path is None or len(g) == 0:
        console.print(f"[red]Error:[/red] {slug!r} not found (HTML or graph missing).")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ttl", delete=False) as tf:
        graph_path = Path(tf.name)
    g.serialize(destination=str(graph_path), format="turtle")

    report = coverage_for_files(html_path, graph_path)

    console.print(f"\n[bold]Coverage[/bold]  {slug}")
    console.print(f"  HTML:  [dim]{html_path.relative_to(project_root)}[/dim]")
    console.print(f"  Graph: [dim]{graph_path.relative_to(project_root)}[/dim]\n")

    if report.total == 0:
        console.print("  [yellow]No atomic units found in HTML.[/yellow]")
        return

    pct_color = "green" if report.percent >= 80 else ("yellow" if report.percent >= 50 else "red")
    console.print(
        f"  Atomic units cited: "
        f"[bold]{report.covered}[/bold] / [bold]{report.total}[/bold]  "
        f"[{pct_color}]({report.percent:.0f}%)[/{pct_color}]"
    )
    n_class_cites = sum(1 for c in report.citations if c.startswith("class-"))
    n_id_cites    = sum(1 for c in report.citations if c.startswith("id-"))
    console.print(
        f"  Citation fragments: [bold]{n_id_cites}[/bold] id-N, "
        f"[bold]{n_class_cites}[/bold] class-N\n"
    )

    uncovered = report.uncovered
    if uncovered:
        from rich.markup import escape as _esc
        console.print(f"[bold]Uncovered atomic units[/bold]  ({len(uncovered)})")
        for u in uncovered:
            section = f"  [dim]({_esc(u.section)})[/dim]" if u.section else ""
            text = _esc(u.text) if u.text else "[dim](empty)[/dim]"
            cls = f"  [dim].{u.css_class}[/dim]" if u.css_class else ""
            console.print(f"  #[bold]{u.id_}[/bold] <{u.tag}> {text}{cls}{section}")
        console.print()

    sections: dict[str | None, list[int]] = {}   # section → [covered, total]
    for u in report.units:
        sec_bucket = sections.setdefault(u.section, [0, 0])
        sec_bucket[1] += 1
        if u.id_ in report.covered_ids:
            sec_bucket[0] += 1
    if any(s for s in sections if s):
        from rich.markup import escape as _esc
        console.print("[bold]By section[/bold]")
        for sec, (cov, tot) in sorted(sections.items(), key=lambda kv: (kv[0] or "")):
            label = _esc(sec) if sec else "[dim](no enclosing data-note)[/dim]"
            color = "green" if cov == tot else ("yellow" if cov > 0 else "red")
            console.print(f"  [{color}]{cov}/{tot}[/{color}]  {label}")
