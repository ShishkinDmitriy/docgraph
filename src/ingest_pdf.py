"""Ingest a PDF source: register file + convert + classify + extract.

The graph file is TriG (one file, two contexts):

    {                                  # default graph: deterministic metadata
      <source/<slug>>      a lis:InformationObject, prov:Entity ;
          dcterms:title "..." ; dg:pageCount N ; prov:generatedAtTime ... .
      <source/<slug>-md>   a lis:InformationObject, prov:Entity ;
          prov:wasDerivedFrom <source/<slug>> ; dg:fileHash "..." .
      <act/conv-<slug>>    a prov:Activity ;
          prov:used <source/<slug>> ;
          prov:generated <source/<slug>-md> ;
          prov:wasAssociatedWith <agent/...> .
      <act/classify-<slug>> a prov:Activity ;
          prov:used <source/<slug>-md> ;
          dg:confidence 0.9 ; dg:reason "..." .
      <act/extract-<slug>>  a prov:Activity ;
          prov:used <source/<slug>-md> .
      <ext/<slug>>         a prov:Entity ;
          prov:wasGeneratedBy <act/classify-<slug>>, <act/extract-<slug>> ;
          dg:confidence 0.9 .
    }

    <ext/<slug>> {                     # named graph: every LLM-derived triple
      <source/<slug>>  a fin:DemandForPayment ;
          fin:totalAmount 150.00 ; ... .
      <source/<slug>/issuer> a foaf:Agent ; fin:legalName "..." ; ... .
    }
"""

from datetime import datetime, timezone
from pathlib import Path

from rdflib import Dataset, Graph, Literal, Namespace, URIRef, RDF, RDFS, XSD
from rich.console import Console

from src.classifier import pdf_to_markdown
from src.classify import classify_document_type, information_object_subclasses
from src.extract import (
    class_def, emit_triples, extract_instance_data, nested_class_defs,
)
from src.extractor import extract_pdf
from src.ingest import (
    DG, LIS, SOURCE_NS,
    IngestError, _check_existing, _mime_type, _register_source, _unique_slug,
    compute_hash, load_combined, make_slug,
)
from src.markdown_io import md_paths_for_pdf, save_markdown
from src.models import ModelConfig
from src.pdfinfo import pdfinfo
from src.project import (
    GRAPHS_SUBDIR, cache_dir, graphs_dir, sources_path,
)

PROV    = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
ACT_NS   = Namespace("http://example.org/docgraph/activity/")
AGENT_NS = Namespace("http://example.org/docgraph/agent/")
EXT_NS   = Namespace("http://example.org/docgraph/extraction/")

GRAPH_SUFFIX = ".trig"


def ingest_pdf(
    source: Path,
    project_root: Path,
    console: Console,
    *,
    client,
    model: ModelConfig,
    note: str | None = None,
    force: bool = False,
    reconvert: bool = False,
) -> Path:
    """Ingest a PDF source: validate, convert, classify, extract, write the graph file.

    Returns the path of the created graph file (TriG).

    *force*       — drop any existing entry for this file (keyed by hash) and
                    re-do classify + extract. Cached markdown is reused if
                    present (saves a vision-model call).
    *reconvert*   — implies *force*; also drops the cached markdown so the
                    PDF→Markdown conversion runs again.
    """
    if reconvert:
        force = True

    source = source.resolve()
    if not source.is_file():
        raise IngestError(f"{source} is not a file")
    if source.suffix.lower() != ".pdf":
        raise IngestError(f"{source.suffix} is not a PDF")

    file_hash = compute_hash(source)
    file_size = source.stat().st_size

    reg = Graph()
    reg.parse(sources_path(project_root), format="turtle")
    _check_existing(reg, project_root, file_hash, force=force, console=console)

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
    if reconvert:
        for md in md_paths_for_pdf(source, cache):
            md.unlink()
            console.print(f"  [yellow]--reconvert[/yellow]: dropped cache "
                          f"[dim]{md.name}[/dim]")
    conv_started = _now()
    console.print("  converting PDF → Markdown...")
    pdf_block = extract_pdf(source)
    docs = pdf_to_markdown(pdf_block, client, model, note=note)
    save_markdown(source, docs, console, cache)
    conv_ended = _now()
    md_files = md_paths_for_pdf(source, cache)
    if not md_files:
        raise IngestError("conversion produced no markdown files")

    # ── Build the dataset (default graph + extraction named graph) ──
    ds = Dataset()
    _bind_prefixes(ds)
    ext_uri      = URIRef(EXT_NS[slug])
    extraction_g = ds.graph(ext_uri)

    # PDF-specific metadata in DEFAULT graph
    ds.add((pdf_uri, RDF.type, LIS.InformationObject))
    ds.add((pdf_uri, RDF.type, PROV.Entity))
    _add_pdfinfo_triples(ds, pdf_uri, info)

    # Markdown derivative(s) in DEFAULT graph
    md_uris = []
    for i, md_path in enumerate(md_files, 1):
        md_uri = URIRef(SOURCE_NS[f"{slug}-md-{i}"]) if len(md_files) > 1 \
                 else URIRef(SOURCE_NS[f"{slug}-md"])
        md_uris.append(md_uri)
        ds.add((md_uri, RDF.type,    LIS.InformationObject))
        ds.add((md_uri, RDF.type,    PROV.Entity))
        ds.add((md_uri, RDFS.label,  Literal(md_path.name)))
        ds.add((md_uri, DG.filePath, Literal(str(md_path.relative_to(project_root)))))
        ds.add((md_uri, DG.fileHash, Literal(compute_hash(md_path))))
        ds.add((md_uri, DG.fileSize, Literal(md_path.stat().st_size, datatype=XSD.integer)))
        ds.add((md_uri, DG.mimeType, Literal(_mime_type(md_path))))
        ds.add((md_uri, PROV.wasDerivedFrom, pdf_uri))

    # Conversion + agent in DEFAULT graph
    conv_uri  = URIRef(ACT_NS[f"conv-{slug}"])
    agent_uri = URIRef(AGENT_NS[make_slug(model.model_id)])
    _add_activity(ds, conv_uri, "PDF to Markdown conversion",
                  used=[pdf_uri], generated=md_uris, agent=agent_uri,
                  started=conv_started, ended=conv_ended)
    ds.add((agent_uri, RDF.type,    PROV.SoftwareAgent))
    ds.add((agent_uri, RDFS.label,  Literal(model.label)))
    ds.add((agent_uri, DG.provider, Literal(model.provider)))
    ds.add((agent_uri, DG.modelId,  Literal(model.model_id)))

    # ── Step 4a: classify ──
    combined = load_combined(project_root)
    candidates = information_object_subclasses(combined)
    console.print(f"  classifying against [bold]{len(candidates)}[/bold] candidate type(s)...")
    markdown = "\n\n---\n\n".join(d.get("markdown", "") for d in docs)

    classify_uri     = URIRef(ACT_NS[f"classify-{slug}"])
    classify_started = _now()
    choice           = classify_document_type(markdown, candidates, client, model)
    classify_ended   = _now()
    _add_activity(ds, classify_uri, "Classify document type",
                  used=md_uris, agent=agent_uri,
                  started=classify_started, ended=classify_ended,
                  confidence=choice.confidence, reason=choice.reason)

    extract_uri = None
    added_props = 0
    if choice.uri is not None:
        extraction_g.add((pdf_uri, RDF.type, choice.uri))
        local = str(choice.uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        console.print(f"  classified as [bold]{local}[/bold] "
                      f"({choice.confidence:.0%}): [dim]{choice.reason}[/dim]")

        # ── Step 7: extract instance properties ──
        root = class_def(combined, choice.uri)
        nested = nested_class_defs(combined, root.properties)
        nested_count = sum(len(c.properties) for c in nested.values())
        console.print(
            f"  extracting [bold]{len(root.properties)}[/bold] direct + "
            f"[bold]{nested_count}[/bold] nested propert(y/ies)..."
        )
        extract_uri     = URIRef(ACT_NS[f"extract-{slug}"])
        extract_started = _now()
        data            = extract_instance_data(markdown, root, nested, client, model)
        extract_ended   = _now()
        _add_activity(ds, extract_uri, "Extract instance properties",
                      used=md_uris, agent=agent_uri,
                      started=extract_started, ended=extract_ended)
        added_props = emit_triples(extraction_g, pdf_uri, data, root, nested,
                                   base_uri=str(pdf_uri))
        console.print(f"  extracted [bold]{added_props}[/bold] property triple(s)")
    else:
        console.print(f"  [yellow]no specific type matched[/yellow] "
                      f"({choice.confidence:.0%}): [dim]{choice.reason}[/dim]")

    # ── Describe the extraction graph itself in the DEFAULT graph ──
    ds.add((ext_uri, RDF.type,        PROV.Entity))
    ds.add((ext_uri, RDFS.label,      Literal(f"LLM-extracted facts about {slug}")))
    ds.add((ext_uri, PROV.wasGeneratedBy, classify_uri))
    if extract_uri is not None:
        ds.add((ext_uri, PROV.wasGeneratedBy, extract_uri))
    ds.add((ext_uri, DG.confidence,   Literal(choice.confidence, datatype=XSD.decimal)))

    # ── Serialize as TriG ──
    graph_file = g_dir / f"{slug}{GRAPH_SUFFIX}"
    ds.serialize(destination=str(graph_file), format="trig")
    total_triples = sum(len(g) for g in ds.graphs())
    console.print(
        f"  wrote   [dim]{GRAPHS_SUBDIR}/{slug}{GRAPH_SUFFIX}[/dim] "
        f"({total_triples} triples; {len(extraction_g)} in extraction graph)"
    )

    _register_source(
        project_root, slug, source, graph_file,
        file_hash=file_hash, file_size=file_size, mime_type=_mime_type(source),
    )
    console.print(f"  registered as [bold]{slug}[/bold]")
    return graph_file


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _bind_prefixes(ds: Dataset) -> None:
    ds.bind("dg",      DG)
    ds.bind("lis",     LIS)
    ds.bind("prov",    PROV)
    ds.bind("dcterms", DCTERMS)
    ds.bind("src",     SOURCE_NS)
    ds.bind("act",     ACT_NS)
    ds.bind("agent",   AGENT_NS)
    ds.bind("ext",     EXT_NS)


def _add_activity(
    ds: Dataset,
    uri: URIRef,
    label: str,
    *,
    used: list[URIRef] | None = None,
    generated: list[URIRef] | None = None,
    agent: URIRef | None = None,
    started=None,
    ended=None,
    confidence: float | None = None,
    reason: str | None = None,
) -> None:
    """Add a prov:Activity description to the default graph."""
    ds.add((uri, RDF.type,   PROV.Activity))
    ds.add((uri, RDFS.label, Literal(label)))
    for u in (used or []):
        ds.add((uri, PROV.used, u))
    for g in (generated or []):
        ds.add((uri, PROV.generated, g))
    if agent is not None:
        ds.add((uri, PROV.wasAssociatedWith, agent))
    if started is not None:
        ds.add((uri, PROV.startedAtTime, Literal(started.isoformat(), datatype=XSD.dateTime)))
    if ended is not None:
        ds.add((uri, PROV.endedAtTime,   Literal(ended.isoformat(),   datatype=XSD.dateTime)))
    if confidence is not None:
        ds.add((uri, DG.confidence, Literal(confidence, datatype=XSD.decimal)))
    if reason:
        ds.add((uri, DG.reason, Literal(reason)))


def _add_pdfinfo_triples(ds: Dataset, subject: URIRef, info: dict[str, str]) -> None:
    """Map pdfinfo's key/value output onto dcterms:/prov:/dg: triples."""
    if not info:
        return

    if (title := info.get("Title")):
        ds.add((subject, DCTERMS.title, Literal(title)))
    if (author := info.get("Author")):
        ds.add((subject, DCTERMS.creator, Literal(author)))
    if (creator := info.get("Creator")):
        ds.add((subject, DCTERMS.creator, Literal(creator)))
    if (producer := info.get("Producer")):
        ds.add((subject, DG.pdfProducer, Literal(producer)))

    pages = info.get("Pages")
    if pages and pages.isdigit():
        ds.add((subject, DG.pageCount, Literal(int(pages), datatype=XSD.integer)))

    for src_key, predicate in (
        ("CreationDate", PROV.generatedAtTime),
        ("ModDate",      DCTERMS.modified),
    ):
        if (raw := info.get(src_key)):
            ds.add((subject, predicate, Literal(_iso_dateTime(raw), datatype=XSD.dateTime)))


def _iso_dateTime(s: str) -> str:
    """Pad pdfinfo's "+02" timezone tail to "+02:00" so xsd:dateTime is valid."""
    if len(s) >= 3 and s[-3] in "+-" and s[-2:].isdigit():
        return s + ":00"
    return s
