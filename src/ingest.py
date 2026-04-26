"""Ingest sources into a docgraph project.

For TTL inputs: symlink ``graphs/<slug>.ttl`` to the original file and register
the source in ``sources.ttl``. No translation step (Part 14 is OWL-native).

For other formats (PDF, Markdown, etc.) the extraction pipeline will write a
real TTL file at ``graphs/<slug>.ttl`` — not implemented yet.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Dataset, Graph, Literal, Namespace, URIRef, RDF, RDFS, XSD
from rich.console import Console

from src.project import (
    GRAPHS_SUBDIR,
    dcterms_path,
    graphs_dir,
    lis14_path,
    meta_path,
    prov_o_path,
    sources_path,
)

DG = Namespace("http://example.org/docgraph/meta#")
LIS = Namespace("http://standards.iso.org/iso/15926/part14/")
SOURCE_NS = Namespace("http://example.org/docgraph/source/")

TTL_SUFFIXES = {".ttl", ".n3"}

# MIME types we recognise locally — keeps ingest deterministic without sniffing.
_MIME_BY_SUFFIX = {
    ".ttl":   "text/turtle",
    ".n3":    "text/n3",
    ".pdf":   "application/pdf",
    ".md":    "text/markdown",
    ".txt":   "text/plain",
}


class IngestError(Exception):
    pass


def make_slug(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return s or "source"


def _unique_slug(base: str, graphs: Path) -> str:
    """Return *base*, or base-2, base-3, ... if a graph file already uses it."""
    candidate = base
    n = 2
    while (graphs / f"{candidate}.ttl").exists() or (graphs / f"{candidate}.trig").exists():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def compute_hash(path: Path, *, chunk: int = 1 << 20) -> str:
    """Return ``sha256:<hex>`` for the file's content."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return f"sha256:{h.hexdigest()}"


def _mime_type(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream")


def _existing_by_hash(reg: Graph, file_hash: str) -> URIRef | None:
    for record in reg.subjects(DG.fileHash, Literal(file_hash)):
        return record  # at most one expected — first one wins
    return None


def remove_source(project_root: Path, slug: str) -> None:
    """Delete a source's graph file (real or symlink) and its registry entry."""
    g_dir = graphs_dir(project_root)
    graph_file = g_dir / f"{slug}.ttl"
    if graph_file.is_symlink() or graph_file.exists():
        graph_file.unlink()

    reg_path = sources_path(project_root)
    reg = Graph()
    reg.parse(reg_path, format="turtle")
    record = URIRef(SOURCE_NS[slug])
    reg.remove((record, None, None))
    reg.serialize(destination=str(reg_path), format="turtle")


def _check_existing(
    reg: Graph,
    project_root: Path,
    file_hash: str,
    *,
    force: bool,
    console: Console,
) -> str | None:
    """If *file_hash* is already registered, either raise or drop the entry.

    Returns the dropped slug when force-removed, else None. The on-disk
    sources.ttl is updated; callers that hold an in-memory copy must reload.
    """
    existing = _existing_by_hash(reg, file_hash)
    if existing is None:
        return None

    slug = str(existing).rsplit("/", 1)[-1]
    existing_path = reg.value(existing, DG.filePath)

    if not force:
        raise IngestError(
            f"this file's content is already ingested as {slug!r} "
            f"(at {existing_path}). Use --force to re-add."
        )

    console.print(
        f"  [yellow]--force[/yellow]: dropping existing entry "
        f"[bold]{slug}[/bold] [dim]({existing_path})[/dim]"
    )
    remove_source(project_root, slug)
    return slug


def ingest_ttl(
    source: Path,
    project_root: Path,
    console: Console,
    *,
    force: bool = False,
) -> Path:
    """Ingest a TTL source: validate, symlink into graphs/, register.

    Returns the path of the created graph file (symlink).
    """
    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"{source} is not a file")
    if source.suffix.lower() not in TTL_SUFFIXES:
        raise IngestError(f"{source.suffix} is not a recognised RDF Turtle extension")

    file_hash = compute_hash(source)
    file_size = source.stat().st_size

    reg = Graph()
    reg.parse(sources_path(project_root), format="turtle")
    _check_existing(reg, project_root, file_hash, force=force, console=console)

    # Sanity-check parse.
    g = Graph()
    try:
        g.parse(source, format="turtle")
    except Exception as exc:
        raise IngestError(f"failed to parse {source}: {exc}") from exc
    triple_count = len(g)

    g_dir = graphs_dir(project_root)
    slug = _unique_slug(make_slug(source.stem), g_dir)
    graph_file = g_dir / f"{slug}.ttl"

    graph_file.symlink_to(source)
    console.print(
        f"  symlink [dim]{GRAPHS_SUBDIR}/{slug}.ttl[/dim] → [dim]{source}[/dim]"
    )

    _register_source(
        project_root, slug, source, graph_file,
        file_hash=file_hash, file_size=file_size, mime_type=_mime_type(source),
    )
    console.print(
        f"  registered as [bold]{slug}[/bold] "
        f"([dim]{triple_count} triple(s), {file_size} bytes[/dim])"
    )
    return graph_file


def _register_source(
    project_root: Path,
    slug: str,
    source: Path,
    graph_file: Path,
    *,
    file_hash: str,
    file_size: int,
    mime_type: str,
) -> None:
    """Append a dual-typed (dg:IngestionRecord + lis:InformationObject) entry to sources.ttl."""
    reg_path = sources_path(project_root)
    reg = Graph()
    reg.bind("dg",  DG)
    reg.bind("lis", LIS)
    reg.parse(reg_path, format="turtle")

    record = URIRef(SOURCE_NS[slug])
    if (record, RDF.type, DG.IngestionRecord) in reg:
        raise IngestError(
            f"sources.ttl already has an entry for slug {slug!r}; "
            f"cannot register {source}"
        )

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    reg.add((record, RDF.type,        DG.IngestionRecord))
    reg.add((record, RDF.type,        LIS.InformationObject))
    reg.add((record, RDFS.label,      Literal(source.name)))
    reg.add((record, DG.filePath,     Literal(str(source))))
    reg.add((record, DG.fileHash,     Literal(file_hash)))
    reg.add((record, DG.fileSize,     Literal(file_size, datatype=XSD.integer)))
    reg.add((record, DG.mimeType,     Literal(mime_type)))
    reg.add((record, DG.graphFile,    Literal(str(graph_file.relative_to(project_root)))))
    reg.add((record, DG.addedAt,      Literal(now, datatype=XSD.dateTime)))

    reg.serialize(destination=str(reg_path), format="turtle")


def list_sources(project_root: Path) -> list[dict]:
    """Return the registry as a list of dicts (for status command)."""
    reg = Graph()
    reg.parse(sources_path(project_root), format="turtle")
    out = []
    for record in reg.subjects(RDF.type, DG.IngestionRecord):
        out.append({
            "uri":         str(record),
            "slug":        str(record).rsplit("/", 1)[-1],
            "label":       str(reg.value(record, RDFS.label)    or ""),
            "sourcePath":  str(reg.value(record, DG.filePath)   or ""),
            "graphFile":   str(reg.value(record, DG.graphFile)  or ""),
            "addedAt":     str(reg.value(record, DG.addedAt)    or ""),
            "fileSize":    int(reg.value(record, DG.fileSize)   or 0),
            "mimeType":    str(reg.value(record, DG.mimeType)   or ""),
            "fileHash":    str(reg.value(record, DG.fileHash)   or ""),
        })
    return sorted(out, key=lambda r: r["addedAt"])


def load_combined(project_root: Path) -> Dataset:
    """Load meta + every bundled upper ontology + every graphs/*.ttl into a Dataset.

    The default graph holds meta.ttl, lis-14.ttl, prov-o.ttl, and dcterms.ttl
    (the permanent backbone). Each ingested source lives in its own named graph.
    """
    # default_union=True: SPARQL queries without explicit FROM clauses see the
    # union of every graph in the dataset — needed so subclasses defined in
    # named graphs (i.e. ingested sources) participate in classification.
    ds = Dataset(default_union=True)
    ds.parse(meta_path(project_root),    format="turtle")
    ds.parse(lis14_path(project_root),   format="turtle")
    ds.parse(prov_o_path(project_root),  format="turtle")
    ds.parse(dcterms_path(project_root), format="turtle")
    g_dir = graphs_dir(project_root)
    for f in sorted(g_dir.iterdir()):
        if f.suffix == ".ttl":
            # Symlinked TTL imports: each file gets its own named graph keyed
            # by file URI so cascade-delete removes a single named graph.
            ds.graph(URIRef(f"file://{f.resolve()}")).parse(f, format="turtle")
        elif f.suffix == ".trig":
            # PDF-derived sources: the file already declares its own named
            # graphs (default + extraction). Parse it as a Dataset.
            ds.parse(f, format="trig")
    return ds
