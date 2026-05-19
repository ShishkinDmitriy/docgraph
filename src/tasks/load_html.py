"""load_html — init task: derive ctx intermediates from converted.html.

No dirty check, runs once per `run()` call. Lifts the implicit
"convert populated ctx with full_markdown/id_to_class/etc." contract
into an explicit declared dependency: every downstream task that
needs those values has `load_html` as a (transitive) upstream dep.

Early-returns when ctx is already populated (convert just ran), so
it's free in the common case. When convert was clean this invocation,
loads converted.html from disk + recomputes the markdown view,
class maps, agent URI, etc.
"""

from __future__ import annotations

from src.html_io import (
    build_class_maps,
    html_paths,
    load_html as _load_html_files,
    render_markdown_view,
)
from src.project import converted_md_path
from src.sources import IngestError
from src.tasks._registry import docgraph


@docgraph.task("load_html", deps=("convert",))
def load_html_task(ctx) -> None:
    if "full_markdown" in ctx:
        return                              # convert just populated it

    docs_raw = _load_html_files(ctx["sd"])
    if not docs_raw:
        raise IngestError(
            f"no converted HTML found for {ctx['slug']!r}; "
            f"run `dg add ... -f convert` first")
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
