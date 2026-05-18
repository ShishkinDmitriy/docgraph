"""register — write the source entry to sources.ttl.

sources.ttl is the project-wide IngestionRecord registry (not a
per-doc delta — different scope). After register runs, the source is
discoverable to `dg status`, `dg list`, etc. via its file_hash.

Dirty check: clean iff sources.ttl already has an entry for this
file_hash. Content match — guards against the registry drifting from
the doc deltas.
"""

from __future__ import annotations

from rdflib import Graph

from src.deltas import delta_path, doc_scope
from src.project import sources_path
from src.sources import existing_by_hash, register_source
from src.tasks._registry import add_registry


@add_registry.task("register", deps=("align",))
def register(ctx) -> None:
    first_delta = delta_path(ctx["project_root"], doc_scope(ctx["slug"]), 1)
    register_source(
        ctx["project_root"], ctx["slug"], ctx["source"], first_delta,
        file_hash=ctx["file_hash"], file_size=ctx["file_size"],
        mime_type="application/pdf",
    )


@add_registry.dirty("register")
def register_dirty(ctx) -> bool:
    reg = Graph()
    reg.parse(sources_path(ctx["project_root"]), format="turtle")
    return existing_by_hash(reg, ctx["file_hash"]) is None
