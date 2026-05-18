"""identity — init task: resolve all per-doc identifiers from source.

Always runs (no dirty check), exactly once per `run()` call. Every
downstream task depends on the ctx fields this populates:

  slug, file_uri, doc_uri, html_uri, md_uri, base_ns, sd  — doc identity
  file_hash, file_size                                     — file identity
  agent_uri                                                — LLM agent URI

Hash-based slug routing: if any prior ingest's sources.ttl entry has
this file's hash, reuse that slug — the doc graph is keyed to THIS
content, not the filename. Otherwise mint a fresh slug from the file
stem.
"""

from __future__ import annotations

from rdflib import Graph, Namespace, URIRef

from src.project import DOCGRAPH_DIR, DOCS_SUBDIR, doc_dir, sources_path
from src.sources import (
    SOURCE_NS,
    compute_hash,
    existing_by_hash,
    make_slug,
    unique_slug,
)
from src.tasks._registry import add_registry

AGENT_NS = Namespace("urn:docgraph:agent:")


@add_registry.task("identity")
def identity(ctx) -> None:
    if "slug" in ctx:
        return                              # idempotent (rare re-call)
    source = ctx["source"]
    ctx["file_hash"] = compute_hash(source)
    ctx["file_size"] = source.stat().st_size

    reg = Graph()
    reg.parse(sources_path(ctx["project_root"]), format="turtle")
    existing = existing_by_hash(reg, ctx["file_hash"])

    docs_root = ctx["project_root"] / DOCGRAPH_DIR / DOCS_SUBDIR
    docs_root.mkdir(parents=True, exist_ok=True)

    if existing is not None:
        ctx["slug"] = str(existing).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    else:
        ctx["slug"] = unique_slug(make_slug(source.stem), docs_root)

    base_ns         = Namespace(f"{SOURCE_NS}{ctx['slug']}/")
    ctx["base_ns"]  = base_ns
    ctx["file_uri"] = URIRef(SOURCE_NS[ctx["slug"]])
    ctx["doc_uri"]  = URIRef(base_ns["doc"])
    ctx["html_uri"] = URIRef(base_ns["html"])
    ctx["md_uri"]   = URIRef(base_ns["md"])
    ctx["sd"]       = doc_dir(ctx["project_root"], ctx["slug"])
    ctx["sd"].mkdir(parents=True, exist_ok=True)

    # Agent URI is invariant for the whole run (depends only on the
    # configured model). Minted here so downstream tasks read
    # ctx["agent_uri"] without re-doing the slugify dance.
    ctx["agent_uri"] = URIRef(AGENT_NS[make_slug(ctx["model"].model_id)])
