"""view — generate and open an annotated HTML view of a doc.

Builds `.docgraph/docs/<slug>/annotated.html` from the canonical HTML
+ extract graph: every cited element gets a green highlight + entity
label badge; uncovered atomic units stay outlined dashed-red.

ctx contract:
    project_root — required (via resolve_project)
    slug         — required (via resolve_slug)
    console      — required
    no_open      — optional (default False; skip browser launch when True)
"""

from __future__ import annotations

import sys
import tempfile
import webbrowser
from pathlib import Path

from src.annotated_view import render_annotated_view
from src.deltas import doc_scope, materialize
from src.html_io import html_paths
from src.project import annotated_html_path, doc_dir
from src.tasks._registry import docgraph


@docgraph.task(deps=("resolve_slug",))
def view(ctx) -> None:
    project_root = ctx["project_root"]
    slug         = ctx["slug"]
    console      = ctx["console"]
    no_open      = ctx.get("no_open", False)

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

    out_path = annotated_html_path(project_root, slug)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    annotated = render_annotated_view(html_path, graph_path, title=slug)
    out_path.write_text(annotated, encoding="utf-8")
    console.print(f"  wrote   [dim]{out_path.relative_to(project_root)}[/dim]")

    if not no_open:
        webbrowser.open(out_path.as_uri())
