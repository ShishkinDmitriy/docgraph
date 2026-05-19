"""identity — resolve all per-doc identifiers (slug-based or file-based).

Foundational task for the per-doc pipeline. Two entry paths:

  1. **File-based** (e.g. `dg add foo.pdf`): ctx["path"] is the PDF.
     Compute file_hash + file_size from disk, look up an existing slug
     in sources.ttl by hash, or mint a fresh one from the file stem.

  2. **Slug-based** (e.g. `dg snapshot demo` or `dg diagram demo`):
     resolve_slug has already populated ctx["slug"] from sources.ttl.
     We don't have the original file (might be gone) but we have its
     recorded hash/size in sources.ttl — read them from there.

Either way, the same downstream-visible ctx fields end up populated:

  slug, file_uri, doc_uri, html_uri, md_uri, base_ns, sd  — doc identity
  file_hash, file_size                                     — file identity
  agent_uri                                                — LLM agent URI

Hash-based slug routing in the file-based path: if any prior ingest's
sources.ttl entry has the same hash, reuse that slug — the doc graph
is keyed to THIS content, not the filename.
"""

from __future__ import annotations

from rdflib import Graph, Namespace, URIRef

from src.project import DOCGRAPH_DIR, DOCS_SUBDIR, doc_dir, sources_path
from src.sources import (
    SOURCE_NS,
    IngestError,
    compute_hash,
    existing_by_hash,
    list_sources,
    make_slug,
    unique_slug,
)
from src.tasks._registry import docgraph

AGENT_NS = Namespace("urn:docgraph:agent:")


@docgraph.task(desc="Resolve per-doc identifiers (slug, URIs, hashes)",
               deps=("resolve_project", "resolve_slug", "setup_llm"))
def identity(ctx) -> None:
    project_root = ctx["project_root"]

    if "slug" in ctx:
        # Slug-based path: doc is already in sources.ttl. Read its
        # recorded hash/size; the original file may not be on disk.
        slug = ctx["slug"]
        record = next((s for s in list_sources(project_root) if s["slug"] == slug),
                      None)
        if record is None:
            raise IngestError(f"slug {slug!r} not registered in sources.ttl")
        ctx["file_hash"] = record["fileHash"]
        ctx["file_size"] = record["fileSize"]
    else:
        # File-based path: validate, hash, mint or reuse slug.
        path = ctx["path"]
        if not path.is_file():
            raise IngestError(f"{path} is not a file")
        if path.suffix.lower() != ".pdf":
            raise IngestError(f"{path.suffix} is not a PDF")
        ctx["file_hash"] = compute_hash(path)
        ctx["file_size"] = path.stat().st_size

        reg = Graph()
        reg.parse(sources_path(project_root), format="turtle")
        existing = existing_by_hash(reg, ctx["file_hash"])

        docs_root = project_root / DOCGRAPH_DIR / DOCS_SUBDIR
        docs_root.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            ctx["slug"] = str(existing).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        else:
            ctx["slug"] = unique_slug(make_slug(path.stem), docs_root)

    slug = ctx["slug"]
    base_ns         = Namespace(f"{SOURCE_NS}{slug}/")
    ctx["base_ns"]  = base_ns
    ctx["file_uri"] = URIRef(SOURCE_NS[slug])
    ctx["doc_uri"]  = URIRef(base_ns["doc"])
    ctx["html_uri"] = URIRef(base_ns["html"])
    ctx["md_uri"]   = URIRef(base_ns["md"])
    ctx["sd"]       = doc_dir(project_root, slug)
    ctx["sd"].mkdir(parents=True, exist_ok=True)

    # Agent URI is invariant for the whole run (depends only on the
    # configured model). Minted here so downstream tasks read
    # ctx["agent_uri"] without re-doing the slugify dance.
    ctx["agent_uri"] = URIRef(AGENT_NS[make_slug(ctx["model"].model_id)])


@docgraph.dirty
def identity_dirty(ctx) -> bool:
    return "file_uri" not in ctx
