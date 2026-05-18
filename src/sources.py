"""sources.ttl — the project-wide registry of ingested sources.

Every ingested source (PDF, TTL, …) gets one entry, keyed by content
hash (the same source path can move on disk; what matters is the bytes).
This module owns:

  - The URN namespace for source IRIs (`SOURCE_NS`).
  - The `IngestError` exception (any ingestion-time error).
  - Slug + hash utilities shared across pipelines.
  - sources.ttl read/write (`register_source`, `existing_by_hash`,
    `list_sources`, `remove_source`).

Not in this module: the per-source pipelines themselves — PDFs go
through `src/tasks/`, TTLs through `src/ttl_ingest.py`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from src.project import sources_path

DG       = Namespace("urn:docgraph:vocab:meta#")
ISO15926 = Namespace("http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#")
SOURCE_NS = Namespace("urn:docgraph:source:")


class IngestError(Exception):
    """Anything that goes wrong while turning an external source into a
    project entry — duplicate hash without --force, malformed input,
    permission errors, etc."""


# ── slug + hash utilities ─────────────────────────────────────────────


def make_slug(name: str) -> str:
    """Slugify a free-form name to `[a-z0-9-]+` for use in URIs / paths."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.lower()).strip("-")
    return s or "source"


def unique_slug(base: str, container: Path) -> str:
    """Return *base*, or base-2, base-3, … if a graph artifact already uses it.

    Looks for collision in either the legacy flat-`graphs/` layout
    (`<slug>.ttl` / `<slug>.trig` siblings) OR the per-doc layout
    (`<container>/<slug>/` subdirectory exists)."""
    candidate = base
    n = 2
    while ((container / f"{candidate}.ttl").exists()
            or (container / f"{candidate}.trig").exists()
            or (container / candidate).is_dir()):
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


# ── sources.ttl I/O ───────────────────────────────────────────────────


def existing_by_hash(reg: Graph, file_hash: str) -> URIRef | None:
    """Look up a registry record by file_hash. Returns the source URI
    if a record with this hash exists, else None."""
    for record in reg.subjects(DG.fileHash, Literal(file_hash)):
        return record
    return None


def register_source(
    project_root: Path,
    slug: str,
    source: Path,
    graph_file: Path,
    *,
    file_hash: str,
    file_size: int,
    mime_type: str,
) -> None:
    """Append a dual-typed (dg:IngestionRecord + iso15926:WholeLifeIndividual)
    entry to sources.ttl."""
    reg_path = sources_path(project_root)
    reg = Graph()
    reg.bind("dg",       DG)
    reg.bind("iso15926", ISO15926)
    reg.parse(reg_path, format="turtle")

    record = URIRef(SOURCE_NS[slug])
    if (record, RDF.type, DG.IngestionRecord) in reg:
        raise IngestError(
            f"sources.ttl already has an entry for slug {slug!r}; "
            f"cannot register {source}"
        )

    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    reg.add((record, RDF.type,    DG.IngestionRecord))
    reg.add((record, RDF.type,    ISO15926.WholeLifeIndividual))
    reg.add((record, RDFS.label,  Literal(source.name)))
    reg.add((record, DG.filePath, Literal(str(source))))
    reg.add((record, DG.fileHash, Literal(file_hash)))
    reg.add((record, DG.fileSize, Literal(file_size, datatype=XSD.integer)))
    reg.add((record, DG.mimeType, Literal(mime_type)))
    reg.add((record, DG.graphFile,
             Literal(str(graph_file.relative_to(project_root)))))
    reg.add((record, DG.addedAt,
             Literal(now, datatype=XSD.dateTime)))

    reg.serialize(destination=str(reg_path), format="turtle")


def list_sources(project_root: Path) -> list[dict]:
    """Return the registry as a list of dicts (used by `dg status`)."""
    reg = Graph()
    reg.parse(sources_path(project_root), format="turtle")
    out = []
    for record in reg.subjects(RDF.type, DG.IngestionRecord):
        out.append({
            "uri":         str(record),
            # URN scheme: `urn:docgraph:source:<slug>` — slug after the
            # last `:`. Legacy http scheme: `…/source/<slug>` — slug
            # after the last `/`. Try `:` first; fall back to `/`.
            "slug":        str(record).rsplit(":", 1)[-1].rsplit("/", 1)[-1],
            "label":       str(reg.value(record, RDFS.label)    or ""),
            "sourcePath":  str(reg.value(record, DG.filePath)   or ""),
            "graphFile":   str(reg.value(record, DG.graphFile)  or ""),
            "addedAt":     str(reg.value(record, DG.addedAt)    or ""),
            "fileSize":    int(reg.value(record, DG.fileSize)   or 0),
            "mimeType":    str(reg.value(record, DG.mimeType)   or ""),
            "fileHash":    str(reg.value(record, DG.fileHash)   or ""),
        })
    return sorted(out, key=lambda r: r["addedAt"])


def remove_source(project_root: Path, slug: str) -> None:
    """Delete a source's per-stage graph files and its registry entry.

    Removes legacy flat-layout `<slug>.ttl` / `<slug>.*.ttl` files that
    may still exist from before the per-doc directory refactor. The
    new per-doc layout (`docs/<slug>/…`) isn't touched here — use
    `shutil.rmtree(doc_dir(project_root, slug))` for that."""
    from src.project import graphs_dir
    g_dir = graphs_dir(project_root)
    candidates = [g_dir / f"{slug}.ttl"]
    candidates.extend(g_dir.glob(f"{slug}.*.ttl"))
    for graph_file in candidates:
        if graph_file.is_symlink() or graph_file.exists():
            graph_file.unlink()

    reg_path = sources_path(project_root)
    reg = Graph()
    reg.parse(reg_path, format="turtle")
    record = URIRef(SOURCE_NS[slug])
    reg.remove((record, None, None))
    reg.serialize(destination=str(reg_path), format="turtle")
