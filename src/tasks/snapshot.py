"""snapshot — write docs/<slug>/graph[.NNN].ttl from materialised deltas.

The doc graph is the canonical artifact: every consumer (diagram, the
viewer, external tooling) reads from `graph.ttl` rather than re-walking
the deltas themselves.

ctx contract:
    project_root — required
    slug         — required
    console      — required
    at_seq       — optional (None = HEAD → graph.ttl; otherwise the
                   historical seq N → graph.NNN.ttl)

Dirty check: clean iff graph[.NNN].ttl exists AND its mtime ≥ the
latest delta's mtime. Historical (at_seq=N) snapshots are content-
addressable — clean as soon as the file exists.
"""

from __future__ import annotations

from src.deltas import doc_scope, materialize
from src.project import doc_dir, graph_ttl_path
from src.tasks._registry import docgraph


@docgraph.task("snapshot", deps=("register",))
def snapshot(ctx) -> None:
    console = ctx["console"]
    at_seq = ctx.get("at_seq")
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


@docgraph.dirty("snapshot")
def snapshot_dirty(ctx) -> bool:
    out_path = graph_ttl_path(ctx["project_root"], ctx["slug"],
                              at_seq=ctx.get("at_seq"))
    if not out_path.exists():
        return True
    if ctx.get("at_seq") is not None:
        return False                                  # historical = content-addressable
    sd = doc_dir(ctx["project_root"], ctx["slug"])
    if not sd.is_dir():
        return False
    deltas = sorted(sd.glob("delta.[0-9][0-9][0-9].trig"))
    if not deltas:
        return False
    return out_path.stat().st_mtime < max(p.stat().st_mtime for p in deltas)
