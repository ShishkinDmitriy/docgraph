"""TTL ingest — the one-shot pipeline for `.ttl`/`.n3` inputs.

PDFs go through the multi-task DAG in `src/tasks/`. TTL sources are
much simpler: validate the file parses, symlink it into
`.docgraph/graphs/<slug>.ttl`, and register the entry in
`sources.ttl`. No LLM, no per-step deltas — the source vocabulary is
already RDF and gets loaded as-is by `extract_part14.loader`.

Called from `main.py:add` when the input file's suffix is in
`TTL_SUFFIXES`. The PDF path is mutually exclusive.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph
from rich.console import Console

from src.project import GRAPHS_SUBDIR, graphs_dir
from src.sources import (
    DG,
    IngestError,
    compute_hash,
    existing_by_hash,
    make_slug,
    register_source,
    remove_source,
    unique_slug,
)

TTL_SUFFIXES = {".ttl", ".n3"}

# MIME types we recognise without sniffing — keeps ingest deterministic.
_MIME_BY_SUFFIX = {
    ".ttl":   "text/turtle",
    ".n3":    "text/n3",
    ".pdf":   "application/pdf",
    ".md":    "text/markdown",
    ".txt":   "text/plain",
}


def _mime_type(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def ingest_ttl(
    source: Path,
    project_root: Path,
    console: Console,
    *,
    force: bool = False,
) -> Path:
    """Ingest a TTL source: validate, symlink into graphs/, register.

    Returns the path of the created graph file (symlink). Raises
    IngestError if the source already exists (use *force* to drop the
    prior entry first)."""
    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"{source} is not a file")
    if source.suffix.lower() not in TTL_SUFFIXES:
        raise IngestError(f"{source.suffix} is not a recognised RDF Turtle extension")

    file_hash = compute_hash(source)
    file_size = source.stat().st_size

    from src.project import sources_path
    reg = Graph()
    reg.parse(sources_path(project_root), format="turtle")
    existing = existing_by_hash(reg, file_hash)
    if existing is not None:
        slug = str(existing).rsplit(":", 1)[-1].rsplit("/", 1)[-1]
        if not force:
            existing_path = reg.value(existing, DG.filePath)
            raise IngestError(
                f"this file's content is already ingested as {slug!r}"
                + (f" (at {existing_path})" if existing_path else "")
                + ". Use --force to re-add."
            )
        console.print(
            f"  [yellow]--force[/yellow]: dropping existing entry "
            f"[bold]{slug}[/bold]"
        )
        remove_source(project_root, slug)

    # Sanity-check parse before linking.
    g = Graph()
    try:
        g.parse(source, format="turtle")
    except Exception as exc:
        raise IngestError(f"failed to parse {source}: {exc}") from exc
    triple_count = len(g)

    g_dir = graphs_dir(project_root)
    slug = unique_slug(make_slug(source.stem), g_dir)
    graph_file = g_dir / f"{slug}.ttl"

    graph_file.symlink_to(source)
    console.print(
        f"  symlink [dim]{GRAPHS_SUBDIR}/{slug}.ttl[/dim] → [dim]{source}[/dim]"
    )

    register_source(
        project_root, slug, source, graph_file,
        file_hash=file_hash, file_size=file_size, mime_type=_mime_type(source),
    )
    console.print(
        f"  registered as [bold]{slug}[/bold] "
        f"([dim]{triple_count} triple(s), {file_size} bytes[/dim])"
    )
    return graph_file


