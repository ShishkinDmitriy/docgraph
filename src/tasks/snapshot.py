"""snapshot — write docs/<slug>/graph[.NNN].ttl from materialised deltas.

The doc graph is the canonical artifact: every consumer (diagram, the
viewer, external tooling) reads from `graph.ttl` rather than re-walking
the deltas themselves.

CLI: `docgraph snapshot TARGET [SEQ]` — TARGET is a slug or path
(resolve_slug handles it); SEQ is optional (HEAD if absent).

ctx contract:
    project_root — required
    slug         — required (via resolve_slug)
    args         — args[1] (optional) is the seq number N → graph.NNN.ttl
    console      — required

Dirty check: clean iff graph[.NNN].ttl exists AND its mtime ≥ the
latest delta's mtime. Historical (seq=N) snapshots are content-
addressable — clean as soon as the file exists.
"""

from __future__ import annotations

from src.deltas import doc_scope, materialize
from src.project import doc_dir, graph_ttl_path
from src.tasks._registry import docgraph


def _at_seq(ctx) -> int | None:
    """Read the optional snapshot seq from ctx (CLI args[1] or
    pre-populated ctx["at_seq"])."""
    if "at_seq" in ctx:
        return ctx["at_seq"]
    args = ctx.get("args", ())
    return int(args[1]) if len(args) >= 2 else None


@docgraph.task(deps=("identity", "register"))
def snapshot(ctx) -> None:
    console = ctx["console"]
    at_seq = _at_seq(ctx)
    g = materialize(ctx["project_root"], doc_scope(ctx["slug"]), at_seq=at_seq)
    if len(g) == 0:
        at_label = f" at seq={at_seq}" if at_seq is not None else ""
        console.print(f"  [yellow]no triples to write[/yellow] for "
                      f"{ctx['slug']}{at_label}")
        return
    out_path = graph_ttl_path(ctx["project_root"], ctx["slug"], at_seq=at_seq)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(out_path), format="turtle")
    console.print(f"  wrote   [dim]{out_path.name}[/dim] ({len(g)} triples)")


@docgraph.dirty
def snapshot_dirty(ctx) -> bool:
    at_seq = _at_seq(ctx)
    out_path = graph_ttl_path(ctx["project_root"], ctx["slug"], at_seq=at_seq)
    if not out_path.exists():
        return True
    if at_seq is not None:
        return False                                  # historical = content-addressable
    sd = doc_dir(ctx["project_root"], ctx["slug"])
    if not sd.is_dir():
        return False
    deltas = sorted(sd.glob("delta.[0-9][0-9][0-9].trig"))
    if not deltas:
        return False
    return out_path.stat().st_mtime < max(p.stat().st_mtime for p in deltas)
