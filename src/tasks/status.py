"""status — print the project's ingested sources.

Project-wide, read-only. Just lists what's in sources.ttl.
"""

from __future__ import annotations

from rich.table import Table

from src.sources import list_sources
from src.tasks._registry import docgraph


@docgraph.task(desc="Show the project's ingested sources", quiet=True,
               deps=("resolve_project",))
def status(ctx) -> None:
    project_root = ctx["project_root"]
    console = ctx["console"]
    sources = list_sources(project_root)
    console.print(f"Sources: [bold]{len(sources)}[/bold]\n")
    if not sources:
        return

    table = Table(show_header=True, header_style="bold cyan", box=None,
                  padding=(0, 2))
    table.add_column("Slug")
    table.add_column("Label")
    table.add_column("Mime",  style="dim")
    table.add_column("Size",  justify="right")
    table.add_column("Added", style="dim")
    for s in sources:
        table.add_row(
            s["slug"], s["label"], s["mimeType"],
            f"{s['fileSize']:,}",
            s["addedAt"][:19],                # trim sub-second / tz tail
        )
    console.print(table)
