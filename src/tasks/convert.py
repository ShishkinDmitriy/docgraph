"""convert — seq 2 delta: PDF → HTML via LLM vision call.

Writes converted.html (canonical, immutable) + converted.md (LLM-prompt
projection) to the per-doc dir and emits an HtmlFile + MarkdownFile
graph that links html_uri/md_uri back to file_uri via
prov:wasDerivedFrom.

Dirty check: clean iff the latest convert delta records this html_uri
as dg:HtmlFile derived from the current file_uri. Re-fires when
upstream identity/recognize sees a different file (different hash =
different slug = different ctx).

Force semantics: when `-f convert` is passed, the LLM is re-run from
scratch — load_or_extract_html drops the cached converted.html so the
vision LLM call happens again. Otherwise the cache is reused even
when this task is dirty for some other reason (no real work to do at
that point).
"""

from __future__ import annotations

from rdflib import Literal
from rdflib.namespace import PROV, RDF, RDFS

from src.deltas import StepDelta, delta_path, doc_scope, next_seq, write_delta
from src.extract_part14.structural import (
    DG,
    build_convert_graph,
)
from src.html_io import (
    build_class_maps,
    html_paths,
    load_or_extract_html,
    render_markdown_view,
)
from src.project import converted_md_path
from src.sources import IngestError
from src.tasks._helpers import (
    latest_delta_of_step,
    now,
    print_delta_summary,
)
from src.tasks._registry import docgraph


@docgraph.task(deps=("recognize",))
def convert(ctx) -> None:
    console = ctx["console"]
    convert_started = now()
    # `-f convert` drops the cached HTML so the vision LLM re-runs;
    # otherwise reuse the cache and just rewrite the convert delta.
    drop_cache = "convert" in ctx.get("forced_tasks", set())
    docs_raw = load_or_extract_html(
        ctx["path"], force=drop_cache, client=ctx["client"],
        model=ctx["model"], con=console, note=ctx.get("note"), html_dir=ctx["sd"],
    )
    convert_ended = now()
    if not docs_raw:
        raise IngestError("conversion produced no HTML documents")

    primary = docs_raw[0]
    document_title       = primary.get("title", "(untitled)")
    document_description = primary.get("description", "") or ""
    full_markdown        = "\n\n---\n\n".join(
        render_markdown_view(d.get("html", "")) for d in docs_raw
    )

    id_to_class:  dict[str, str]      = {}
    class_to_ids: dict[str, set[str]] = {}
    for d in docs_raw:
        i2c, c2i = build_class_maps(d.get("html", ""))
        id_to_class.update(i2c)
        for cls, ids in c2i.items():
            class_to_ids.setdefault(cls, set()).update(ids)

    html_files     = html_paths(ctx["sd"])
    html_file_path = html_files[0] if html_files else None
    md_file_path   = converted_md_path(ctx["project_root"], ctx["slug"])
    md_file_path.write_text(full_markdown, encoding="utf-8")

    # identity already minted agent_uri from the model config.
    agent_uri = ctx["agent_uri"]

    g = build_convert_graph(
        file_uri             = ctx["file_uri"],
        doc_uri              = ctx["doc_uri"],
        html_uri             = ctx["html_uri"],
        html_file_path       = html_file_path,
        md_uri               = ctx["md_uri"],
        md_file_path         = md_file_path,
        project_root         = ctx["project_root"],
        document_title       = document_title,
        document_description = document_description,
        convert_started      = convert_started,
        convert_ended        = convert_ended,
        convert_agent_uri    = agent_uri,
    )
    g.add((agent_uri, RDF.type,    PROV.SoftwareAgent))
    g.add((agent_uri, RDFS.label,  Literal(ctx["model"].label)))
    g.add((agent_uri, DG.provider, Literal(ctx["model"].provider)))
    g.add((agent_uri, DG.modelId,  Literal(ctx["model"].model_id)))

    seq = next_seq(ctx["project_root"], doc_scope(ctx["slug"]))
    write_delta(
        StepDelta(scope=doc_scope(ctx["slug"]), step="convert", seq=seq,
                  added=g, parent_seq=seq - 1, agent=agent_uri,
                  timestamp=convert_ended),
        delta_path(ctx["project_root"], doc_scope(ctx["slug"]), seq),
    )
    print_delta_summary(console, seq, len(g), 0)

    # Hand-off to load_html / extract / templates. load_html will
    # early-return because these are already populated.
    ctx["docs_raw"]             = docs_raw
    ctx["full_markdown"]        = full_markdown
    ctx["id_to_class"]          = id_to_class
    ctx["class_to_ids"]         = class_to_ids
    ctx["html_file_path"]       = html_file_path
    ctx["md_file_path"]         = md_file_path
    ctx["document_title"]       = document_title
    ctx["document_description"] = document_description


@docgraph.dirty
def convert_dirty(ctx) -> bool:
    if "path" not in ctx:
        return False                   # slug-based invocation — no file to convert from
    latest = latest_delta_of_step(ctx, "convert")
    if latest is None:
        return True
    g = latest.added
    html_uri = ctx["html_uri"]
    if (html_uri, RDF.type, DG.HtmlFile) not in g:
        return True
    if (html_uri, PROV.wasDerivedFrom, ctx["file_uri"]) not in g:
        return True
    return False
