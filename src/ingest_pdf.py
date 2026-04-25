"""Ingest a PDF source: register file + convert to Markdown + record provenance.

PR2 first slice — no classification or extraction yet. This produces a complete
provenance chain on disk you can read by eye:

    sources.ttl: dg:IngestionRecord for the original PDF (file metadata, hash)
    graphs/<slug>.ttl:
      <source/<slug>>      a lis:InformationObject, prov:Entity ;
          dcterms:title "..." ; dg:pageCount N ; prov:generatedAtTime ... .
      <source/<slug>-md-N> a lis:InformationObject, prov:Entity ;
          prov:wasDerivedFrom <source/<slug>> ; dg:fileHash "..." .
      <conv/<slug>>        a prov:Activity ;
          prov:used <source/<slug>> ;
          prov:generated <source/<slug>-md-1>, ... ;
          prov:wasAssociatedWith <agent/...> .
      <agent/<slug>>       a prov:SoftwareAgent ;
          dg:provider "anthropic" ; dg:modelId "..." .
"""

from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef, RDF, RDFS, XSD
from rich.console import Console

from src.classifier import pdf_to_markdown
from src.extractor import extract_pdf
from src.ingest import (
    DG, LIS, SOURCE_NS,
    IngestError, _existing_by_hash, _mime_type, _unique_slug,
    compute_hash, make_slug,
)
from src.markdown_io import md_paths_for_pdf, save_markdown
from src.models import ModelConfig
from src.pdfinfo import pdfinfo
from src.project import (
    GRAPHS_SUBDIR, cache_dir, graphs_dir, sources_path,
)

PROV    = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
CONV_NS  = Namespace("http://example.org/docgraph/conversion/")
AGENT_NS = Namespace("http://example.org/docgraph/agent/")


def ingest_pdf(
    source: Path,
    project_root: Path,
    console: Console,
    *,
    client,
    model: ModelConfig,
    note: str | None = None,
) -> Path:
    """Ingest a PDF source: validate, convert, register, write the named graph.

    Returns the path of the created graph file.
    """
    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"{source} is not a file")
    if source.suffix.lower() != ".pdf":
        raise IngestError(f"{source.suffix} is not a PDF")

    file_hash = compute_hash(source)
    file_size = source.stat().st_size

    reg = Graph()
    reg.parse(sources_path(project_root), format="turtle")
    if (existing := _existing_by_hash(reg, file_hash)) is not None:
        slug = str(existing).rsplit("/", 1)[-1]
        existing_path = reg.value(existing, DG.filePath)
        raise IngestError(
            f"this file's content is already ingested as {slug!r} "
            f"(at {existing_path}). Run `docgraph clean` to start over."
        )

    g_dir   = graphs_dir(project_root)
    slug    = _unique_slug(make_slug(source.stem), g_dir)
    pdf_uri = URIRef(SOURCE_NS[slug])

    # ── pdfinfo metadata (local, no LLM) ──
    info = pdfinfo(source)
    if info:
        console.print(f"  pdfinfo: [dim]{info.get('Pages','?')} page(s), "
                      f"{info.get('Title') or '(no title)'}[/dim]")

    # ── Convert PDF → Markdown (LLM call, cached) ──
    cache = cache_dir(project_root)
    cache.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).replace(microsecond=0)
    console.print("  converting PDF → Markdown...")
    pdf_block = extract_pdf(source)
    docs = pdf_to_markdown(pdf_block, client, model, note=note)
    save_markdown(source, docs, console, cache)
    ended = datetime.now(timezone.utc).replace(microsecond=0)
    md_files = md_paths_for_pdf(source, cache)
    if not md_files:
        raise IngestError("conversion produced no markdown files")

    # ── Build the named graph ──
    g = Graph()
    g.bind("dg",      DG)
    g.bind("lis",     LIS)
    g.bind("prov",    PROV)
    g.bind("dcterms", DCTERMS)

    # PDF-specific metadata (sources.ttl already carries hash/size/mime/path)
    g.add((pdf_uri, RDF.type, LIS.InformationObject))
    g.add((pdf_uri, RDF.type, PROV.Entity))
    _add_pdfinfo_triples(g, pdf_uri, info)

    # Markdown derivative(s)
    md_uris = []
    for i, md_path in enumerate(md_files, 1):
        md_uri = URIRef(SOURCE_NS[f"{slug}-md-{i}"]) if len(md_files) > 1 else URIRef(SOURCE_NS[f"{slug}-md"])
        md_uris.append(md_uri)
        g.add((md_uri, RDF.type,    LIS.InformationObject))
        g.add((md_uri, RDF.type,    PROV.Entity))
        g.add((md_uri, RDFS.label,  Literal(md_path.name)))
        g.add((md_uri, DG.filePath, Literal(str(md_path.relative_to(project_root)))))
        g.add((md_uri, DG.fileHash, Literal(compute_hash(md_path))))
        g.add((md_uri, DG.fileSize, Literal(md_path.stat().st_size, datatype=XSD.integer)))
        g.add((md_uri, DG.mimeType, Literal(_mime_type(md_path))))
        g.add((md_uri, PROV.wasDerivedFrom, pdf_uri))

    # Conversion activity
    conv_uri  = URIRef(CONV_NS[slug])
    agent_uri = URIRef(AGENT_NS[make_slug(model.model_id)])
    g.add((conv_uri, RDF.type,            PROV.Activity))
    g.add((conv_uri, RDFS.label,          Literal("PDF to Markdown conversion")))
    g.add((conv_uri, PROV.used,           pdf_uri))
    for md_uri in md_uris:
        g.add((conv_uri, PROV.generated,  md_uri))
    g.add((conv_uri, PROV.startedAtTime,  Literal(started.isoformat(), datatype=XSD.dateTime)))
    g.add((conv_uri, PROV.endedAtTime,    Literal(ended.isoformat(),   datatype=XSD.dateTime)))
    g.add((conv_uri, PROV.wasAssociatedWith, agent_uri))

    # Software agent
    g.add((agent_uri, RDF.type,    PROV.SoftwareAgent))
    g.add((agent_uri, RDFS.label,  Literal(model.label)))
    g.add((agent_uri, DG.provider, Literal(model.provider)))
    g.add((agent_uri, DG.modelId,  Literal(model.model_id)))

    graph_file = g_dir / f"{slug}.ttl"
    g.serialize(destination=str(graph_file), format="turtle")
    console.print(f"  wrote   [dim]{GRAPHS_SUBDIR}/{slug}.ttl[/dim] ({len(g)} triples)")

    # ── sources.ttl: same minimal admin record as TTL flow ──
    from src.ingest import _register_source
    _register_source(
        project_root, slug, source, graph_file,
        file_hash=file_hash, file_size=file_size, mime_type=_mime_type(source),
    )
    console.print(f"  registered as [bold]{slug}[/bold]")
    return graph_file


def _add_pdfinfo_triples(g: Graph, subject: URIRef, info: dict[str, str]) -> None:
    """Map pdfinfo's key/value output onto dcterms:/prov:/dg: triples."""
    if not info:
        return

    if (title := info.get("Title")):
        g.add((subject, DCTERMS.title, Literal(title)))
    if (author := info.get("Author")):
        g.add((subject, DCTERMS.creator, Literal(author)))
    if (creator := info.get("Creator")):
        g.add((subject, DCTERMS.creator, Literal(creator)))
    if (producer := info.get("Producer")):
        g.add((subject, DG.pdfProducer, Literal(producer)))

    pages = info.get("Pages")
    if pages and pages.isdigit():
        g.add((subject, DG.pageCount, Literal(int(pages), datatype=XSD.integer)))

    # pdfinfo -isodates produces "2020-10-08T16:23:21+02" — append :00 if needed for xsd:dateTime
    for src_key, predicate in (
        ("CreationDate", PROV.generatedAtTime),
        ("ModDate",      DCTERMS.modified),
    ):
        if (raw := info.get(src_key)):
            g.add((subject, predicate, Literal(_iso_dateTime(raw), datatype=XSD.dateTime)))


def _iso_dateTime(s: str) -> str:
    """Pad pdfinfo's "+02" timezone tail to "+02:00" so xsd:dateTime is valid."""
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():
        return s + ":00"
    return s
