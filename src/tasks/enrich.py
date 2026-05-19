"""enrich — refine entity types via external RDL and unlock properties.

Operates on an already-extracted doc graph: queries an external RDL
(currently Wikidata as POC, POSC Caesar configured) for more-specific
classes, then re-extracts properties that the refined types unlock.

CLI: `docgraph enrich TARGET` — TARGET is a slug or path.

ctx contract:
    project_root — required (via resolve_project)
    slug         — required (via resolve_slug)
    client, model — required (via setup_llm)
    console      — required
"""

from __future__ import annotations

from src.extract_part14.enrich import enrich_source
from src.extract_part14.rdl import POSC_CAESAR, RdlResolver
from src.project import cache_dir
from src.tasks._registry import docgraph


@docgraph.task(desc="Refine entity types via external RDL",
               deps=("resolve_slug", "setup_llm"))
def enrich(ctx) -> None:
    project_root = ctx["project_root"]
    slug         = ctx["slug"]
    console      = ctx["console"]

    rdl_cache_dir = cache_dir(project_root) / "rdl"
    rdl_resolvers = [RdlResolver(POSC_CAESAR, cache_dir=rdl_cache_dir)]

    try:
        added = enrich_source(
            project_root, slug, rdl_resolvers,
            client=ctx["client"], model=ctx["model"], console=console,
        )
        console.print(f"  → {added} new triple(s)")
    except FileNotFoundError as exc:
        console.print(f"  [yellow]skip[/yellow]: {exc}")
