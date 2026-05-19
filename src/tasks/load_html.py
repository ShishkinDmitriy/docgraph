"""load_html — derive ctx intermediates from converted.html.

Lifts the implicit "convert populated ctx with full_markdown /
id_to_class / etc." contract into an explicit declared dependency:
every downstream task that needs those values has `load_html` as a
(transitive) upstream dep.

Dirty when full_markdown isn't in ctx AND HTML exists on disk to
load. Skip silently when convert just populated everything (common
add-pipeline case) or when there's no HTML at all (slug-based
invocations like snapshot/diagram).
"""

from __future__ import annotations

from src.html_io import (
    build_class_maps,
    html_paths,
    load_html as _load_html_files,
    render_markdown_view,
)
from src.project import converted_md_path
from src.tasks._registry import docgraph


@docgraph.task(deps=("convert",))
def load_html(ctx) -> None:
    docs_raw = _load_html_files(ctx["sd"])
    primary = docs_raw[0]
    ctx["docs_raw"]             = docs_raw
    ctx["document_title"]       = primary.get("title", "(untitled)")
    ctx["document_description"] = primary.get("description", "") or ""
    ctx["full_markdown"] = "\n\n---\n\n".join(
        render_markdown_view(d.get("html", "")) for d in docs_raw
    )
    id_to_class:  dict[str, str]      = {}
    class_to_ids: dict[str, set[str]] = {}
    for d in docs_raw:
        i2c, c2i = build_class_maps(d.get("html", ""))
        id_to_class.update(i2c)
        for cls, ids in c2i.items():
            class_to_ids.setdefault(cls, set()).update(ids)
    ctx["id_to_class"]    = id_to_class
    ctx["class_to_ids"]   = class_to_ids
    html_files            = html_paths(ctx["sd"])
    ctx["html_file_path"] = html_files[0] if html_files else None
    ctx["md_file_path"]   = converted_md_path(ctx["project_root"], ctx["slug"])
    # agent_uri is set by identity (invariant per run).


@docgraph.dirty
def load_html_dirty(ctx) -> bool:
    if "full_markdown" in ctx:
        return False                        # convert just populated it
    return bool(html_paths(ctx["sd"]))      # only run if HTML exists to load
