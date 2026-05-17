"""Ingest a PDF source: register file + convert + classify.

Source typing follows ISO 15926-2 (POSC Caesar OWL) strict mode. The
classify step runs the 14-prompt pipeline in `src.classify_part2`; see
``docs/classify_design.md`` for the design and gating logic.

The graph file is Turtle (one file = one named graph, ext/<slug>):

      <source/<slug>>      a iso15926:WholeLifeIndividual, prov:Entity ;
          dcterms:title "..." ; dg:pageCount N ; prov:generatedAtTime ... .
      <source/<slug>-md>   a iso15926:WholeLifeIndividual, prov:Entity ;
          prov:wasDerivedFrom <source/<slug>> ; dg:fileHash "..." .
      <act/conv-<slug>>    a prov:Activity ; …
      <act/classify-<slug>> a prov:Activity ; …
      <ext/<slug>>         a prov:Entity ;
          dg:scopeCoverage 0.45 ; dg:evidenceCoverage 0.18 ;
          dg:docKind "maintenance procedure" .
      <source/<slug>>  a ext:maintenance-procedure .
      ext:maintenance-procedure  a iso15926:ClassOfInformationObject .
      …all activity / individual / class / property / connection triples…
"""

from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef, RDF, RDFS, XSD
from rich.console import Console

from src.classify_part2 import pipeline as classify_pipeline
from src.classify_part2.context import ConversionContext
from src.classify_part2.ns import EXT_NS_FOR
from src.ingest import (
    DG, ISO15926, SOURCE_NS,
    IngestError, _check_existing, _mime_type, _register_source, _unique_slug,
    compute_hash, make_slug,
)
from src.markdown_io import load_or_extract, md_paths_for_pdf
from src.models import ModelConfig
from src.pdfinfo import pdfinfo
from src.project import (
    DOCGRAPH_DIR, GRAPHS_SUBDIR, cache_dir, graphs_dir, sources_path,
)

PROV    = Namespace("http://www.w3.org/ns/prov#")
DCTERMS = Namespace("http://purl.org/dc/terms/")
ACT_NS   = Namespace("urn:docgraph:activity:")
AGENT_NS = Namespace("urn:docgraph:agent:")
EXT_NS   = Namespace("urn:docgraph:extraction:")

GRAPH_SUFFIX = ".ttl"


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
    """Ingest a PDF source: validate, convert, classify, write the graph file.

    Returns the path of the created graph file (TriG).

    *force*       — drop any existing entry for this file (keyed by hash) and
                    re-run conversion + classify. Cached markdown is reused
                    if present (saves a vision-model call).
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
    docs = load_or_extract(
        source, force=reconvert, client=client, model=model,
        con=console, note=note, cache_dir=cache,
    )
    conv_ended = _now()
    md_files = md_paths_for_pdf(source, cache)
    if not md_files:
        raise IngestError("conversion produced no markdown files")

    # ── Build a single named graph (all triples in one place) ──
    g = Graph()
    _bind_prefixes(g, slug)
    ext_uri = URIRef(EXT_NS[slug])

    g.add((pdf_uri, RDF.type, ISO15926.WholeLifeIndividual))
    g.add((pdf_uri, RDF.type, PROV.Entity))
    _add_pdfinfo_triples(g, pdf_uri, info)

    md_uris = []
    for i, md_path in enumerate(md_files, 1):
        md_uri = URIRef(SOURCE_NS[f"{slug}-md-{i}"]) if len(md_files) > 1 \
                 else URIRef(SOURCE_NS[f"{slug}-md"])
        md_uris.append(md_uri)
        g.add((md_uri, RDF.type,    ISO15926.WholeLifeIndividual))
        g.add((md_uri, RDF.type,    PROV.Entity))
        g.add((md_uri, RDFS.label,  Literal(md_path.name)))
        g.add((md_uri, DG.filePath, Literal(str(md_path.relative_to(project_root)))))
        g.add((md_uri, DG.fileHash, Literal(compute_hash(md_path))))
        g.add((md_uri, DG.fileSize, Literal(md_path.stat().st_size, datatype=XSD.integer)))
        g.add((md_uri, DG.mimeType, Literal(_mime_type(md_path))))
        g.add((md_uri, PROV.wasDerivedFrom, pdf_uri))

    conv_uri  = URIRef(ACT_NS[f"conv-{slug}"])
    agent_uri = URIRef(AGENT_NS[make_slug(model.model_id)])
    _add_activity(g, conv_uri, "PDF to Markdown conversion",
                  used=[pdf_uri], generated=md_uris, agent=agent_uri,
                  started=conv_started, ended=conv_ended)
    g.add((agent_uri, RDF.type,    PROV.SoftwareAgent))
    g.add((agent_uri, RDFS.label,  Literal(model.label)))
    g.add((agent_uri, DG.provider, Literal(model.provider)))
    g.add((agent_uri, DG.modelId,  Literal(model.model_id)))

    # ── Classify pipeline (14 prompts → Part 2 graph) ──
    g.add((ext_uri, RDF.type,   PROV.Entity))
    g.add((ext_uri, RDFS.label, Literal(f"LLM-extracted facts about {slug}")))

    markdown = "\n\n---\n\n".join(d.get("markdown", "") for d in docs)
    ctx = ConversionContext(
        source_uri=pdf_uri,
        source_slug=slug,
        ext_ns=EXT_NS_FOR(slug),
    )
    result = classify_pipeline.classify(
        markdown=markdown, ctx=ctx,
        client=client, model=model, console=console,
    )

    for triple in result.graph:
        g.add(triple)
    # Carry the pipeline's prefix bindings (template prefixes — iso:, rdl:,
    # ex: — get registered there). Without this the merged graph falls back
    # to autogenerated `ns1:` for any namespace not in `_bind_prefixes`.
    for prefix, ns in result.graph.namespaces():
        g.bind(prefix, ns, override=False)

    classify_uri = URIRef(ACT_NS[f"classify-{slug}"])
    _add_activity(g, classify_uri, "Classify document (ISO 15926-2 pipeline)",
                  used=md_uris, agent=agent_uri,
                  started=result.started, ended=result.ended)
    g.add((ext_uri, PROV.wasGeneratedBy, classify_uri))
    classify_pipeline.attach_pipeline_metrics(g, ext_uri=ext_uri, nat=result.nature)
    if result.ran:
        console.print(f"  ran [bold]{len(result.ran)}[/bold] prompt(s); "
                      f"skipped [dim]{len(result.skipped)}[/dim]")

    # ── Serialize as Turtle (one file = named graph ext/<slug>) ──
    graph_file = g_dir / f"{slug}{GRAPH_SUFFIX}"
    g.serialize(destination=str(graph_file), format="turtle")
    console.print(
        f"  wrote   [dim]{GRAPHS_SUBDIR}/{slug}{GRAPH_SUFFIX}[/dim] "
        f"({len(g)} triples)"
    )

    _register_source(
        project_root, slug, source, graph_file,
        file_hash=file_hash, file_size=file_size, mime_type=_mime_type(source),
    )
    # `registered as <slug>` is printed by the caller in main.py after
    # any post-pipeline work, so it stays the very last line.
    return graph_file


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _bind_prefixes(g: Graph, slug: str | None = None) -> None:
    g.bind("dg",       DG)
    g.bind("iso15926", ISO15926)
    g.bind("prov",    PROV)
    g.bind("dcterms", DCTERMS)
    g.bind("src",     SOURCE_NS)
    g.bind("act",     ACT_NS)
    g.bind("agent",   AGENT_NS)
    g.bind("ext",     EXT_NS)
    if slug:
        g.bind("e", Namespace(f"urn:docgraph:extraction:{slug}/"))


def _add_activity(
    g: Graph,
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
    """Add a prov:Activity description to the graph."""
    g.add((uri, RDF.type,   PROV.Activity))
    g.add((uri, RDFS.label, Literal(label)))
    for u in (used or []):
        g.add((uri, PROV.used, u))
    for gen in (generated or []):
        g.add((uri, PROV.generated, gen))
    if agent is not None:
        g.add((uri, PROV.wasAssociatedWith, agent))
    if started is not None:
        g.add((uri, PROV.startedAtTime, Literal(started.isoformat(), datatype=XSD.dateTime)))
    if ended is not None:
        g.add((uri, PROV.endedAtTime,   Literal(ended.isoformat(),   datatype=XSD.dateTime)))
    if confidence is not None:
        g.add((uri, DG.confidence, Literal(confidence, datatype=XSD.decimal)))
    if reason:
        g.add((uri, DG.reason, Literal(reason)))


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
