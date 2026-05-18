"""diagram — render diagram.{puml,svg} from the doc HEAD state.

PlantUML on the public plantuml.com server (best-effort; if the
network call fails the .puml is still on disk and can be rendered
later via `dg diagram <slug>`).

Dirty check: clean iff diagram.puml exists AND its mtime ≥ the latest
delta's mtime (any new delta in the doc dir makes the diagram stale).
File-system check, not graph-content check — diagrams are rendered
artifacts, not part of the RDF model.
"""

from __future__ import annotations

from src.tasks._registry import add_registry


@add_registry.task("diagram", deps=("register",))
def diagram(ctx) -> None:
    from src.diagram import DiagramError, make_diagram
    console = ctx["console"]
    try:
        make_diagram(ctx["project_root"], ctx["slug"], console)
    except DiagramError as exc:
        console.print(f"  [yellow]diagram skipped[/yellow]: {exc}")
    except Exception as exc:
        console.print(f"  [yellow]diagram failed[/yellow]: {exc}")


@add_registry.dirty("diagram")
def diagram_dirty(ctx) -> bool:
    from src.diagram import diagram_is_current
    return not diagram_is_current(ctx["project_root"], ctx["slug"])
