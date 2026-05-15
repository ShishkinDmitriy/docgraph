"""Top-level extract entry point for the part14 pipeline (M1).

M1 deliverable: produce a Part 14 named graph for a PDF source containing
the file → document chain plus subject classification. No chapters, no
quotes — those are minted top-down by M2's branch walker as evidence cited
by extracted entities (see docs/architecture/extraction.md § Quote model).

See ARCHITECTURE.md § Pipelines — Part 14 build-out for the milestone plan.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import PROV, RDF, RDFS, XSD
from rich.console import Console

from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.structural import DG, LIS, build_chain
from src.extract_part14.mega_walker import walk_mega
from src.extract_part14.property_walker import infer_cross_entity_links
from src.extract_part14.template_recognizer import walk_templates
from src.extract_part14.rdl import POSC_CAESAR, RdlResolver
from src.ingest import (
    IngestError,
    SOURCE_NS,
    _check_existing,
    _register_source,
    _unique_slug,
    compute_hash,
    make_slug,
)
from src.html_io import (
    build_class_maps,
    html_paths_for_pdf,
    load_or_extract_html,
    render_markdown_view,
)
from src.markdown_io import md_paths_for_pdf
from src.models import ModelConfig
from src.pdfinfo import pdfinfo
from src.project import (
    GRAPHS_SUBDIR,
    HTML_SUBDIR,
    cache_dir,
    graphs_dir,
    html_dir,
    sources_path,
)

GRAPH_SUFFIX = ".ttl"
AGENT_NS = Namespace("http://example.org/docgraph/agent/")
logger = logging.getLogger(__name__)


def extract_pdf_part14(
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
    """M1 entry point: structural file→doc chain + subject classification.

    *force*     — drop any existing entry for this file (keyed by hash) and
                  re-run extraction. Cached markdown is reused unless
                  *reconvert* is also set.
    *reconvert* — implies *force*; also drops the cached markdown so the
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

    g_dir = graphs_dir(project_root)
    slug  = _unique_slug(make_slug(source.stem), g_dir)
    base_ns  = Namespace(f"{SOURCE_NS}{slug}/")
    file_uri = URIRef(SOURCE_NS[slug])
    doc_uri  = URIRef(base_ns["doc"])
    md_uri   = URIRef(base_ns["md"])

    # ── pdfinfo metadata (local, no LLM) ──
    info = pdfinfo(source)
    if info:
        console.print(f"  pdfinfo: [dim]{info.get('Pages', '?')} page(s), "
                      f"{info.get('Title') or '(no title)'}[/dim]")

    # ── Convert PDF → HTML (cached, canonical, immutable) ──
    # The HTML is the source-of-truth artifact: structure + atomic-unit IDs
    # seeded by the conversion LLM. Extraction passes consume a Markdown
    # view rendered mechanically from the HTML — token-efficient for the
    # LLM, with `{#id-N}` markers per element so evidence cites by anchor.
    # See docs/architecture/html-pipeline.md.
    h_dir = html_dir(project_root)
    h_dir.mkdir(parents=True, exist_ok=True)

    convert_started = _now()
    docs_raw = load_or_extract_html(
        source, force=reconvert, client=client, model=model,
        con=console, note=note, html_dir=h_dir,
    )
    convert_ended = _now()

    if not docs_raw:
        raise IngestError("conversion produced no HTML documents")

    # ── Document title / description / source path ──
    primary = docs_raw[0]
    document_title       = primary.get("title", "(untitled)")
    document_description = primary.get("description", "") or ""
    # Render the markdown view from each HTML document and concatenate. The
    # extraction LLM sees this; anchor markers (`{#id-N}`) point back into
    # the canonical HTML for fragment-URI minting.
    full_markdown        = "\n\n---\n\n".join(
        render_markdown_view(d.get("html", "")) for d in docs_raw
    )

    # ── Build id↔class maps for citation-collapse during extraction ──
    # When all members of a `class-N` group are cited as evidence for one
    # entity, the walker emits a single `<doc#class-N>` triple instead of
    # N per-id triples (cleaner graph, same semantics).
    id_to_class:  dict[str, str]      = {}
    class_to_ids: dict[str, set[str]] = {}
    for d in docs_raw:
        i2c, c2i = build_class_maps(d.get("html", ""))
        id_to_class.update(i2c)
        for cls, ids in c2i.items():
            class_to_ids.setdefault(cls, set()).update(ids)

    # ── Resolve the canonical HTML path for fragment-URI anchoring ──
    html_files = html_paths_for_pdf(source, h_dir)
    md_file_path = html_files[0] if html_files else None

    # ── CONVERT LAYER — file metadata + structural chain + conversion activity ──
    agent_uri = URIRef(AGENT_NS[make_slug(model.model_id)])
    g_convert = build_chain(
        file_path             = source,
        file_uri              = file_uri,
        doc_uri               = doc_uri,
        document_title        = document_title,
        document_description  = document_description,
        project_root          = project_root,
        file_hash             = file_hash,
        file_size             = file_size,
        mime_type             = "application/pdf",
        md_uri                = md_uri,
        md_file_path          = md_file_path,
        pdf_info              = info,
        convert_started       = convert_started,
        convert_ended         = convert_ended,
        convert_agent_uri     = agent_uri,
    )
    g_convert.add((agent_uri, RDF.type,    PROV.SoftwareAgent))
    g_convert.add((agent_uri, RDFS.label,  Literal(model.label)))
    g_convert.add((agent_uri, DG.provider, Literal(model.provider)))
    g_convert.add((agent_uri, DG.modelId,  Literal(model.model_id)))

    # ── EXTRACT LAYER — subject + entities + properties + quotes ──
    # Separate graph from convert; written to a separate file. They share
    # URIs (file_uri etc.) but live in different named graphs so each
    # stage's contribution is provenance-distinct.
    g = Graph()
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("prov", PROV, override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   base_ns, override=True, replace=True)

    # ── Load ontology view (used by both subject classification and walker) ──
    ds       = build_dataset(project_root)
    ontology = union_view(ds)

    # ── EXTRACT — single mega-call: subject + entities + properties + invocations + roles + ext-class proposals ──
    extracted: list = []
    roles:     list = []
    if full_markdown.strip():
        rdl_cache_dir = cache_dir(project_root) / "rdl"
        rdl_resolvers = [RdlResolver(POSC_CAESAR, cache_dir=rdl_cache_dir)]
        result = walk_mega(
            full_markdown   = full_markdown,
            document_title  = document_title,
            document_descr  = document_description,
            base_ns         = base_ns,
            md_source_uri   = md_uri,
            file_uri        = file_uri,
            ontology        = ontology,
            client          = client,
            model           = model,
            id_to_class     = id_to_class,
            class_to_ids    = class_to_ids,
            rdl_resolvers   = rdl_resolvers,
            console         = console,
        )
        for triple in result.graph:
            g.add(triple)
        for prefix, ns in result.graph.namespaces():
            g.bind(prefix, ns, override=False)
        extracted = result.entities
        console.print(f"  → {len(extracted)} entit{'y' if len(extracted) == 1 else 'ies'}, "
                      f"{len(result.new_ext_classes)} new ext class(es)")

    # ── Inferred cross-entity links — fills missing class-ranged property
    # triples by quote co-occurrence (e.g. ScalarQuantityDatum's mention of
    # "EUR" in its supporting quote → lis:datumUOM link to <unitofmeasure/eur>).
    # Deterministic, no LLM. Only fires when the LLM missed an obvious link.
    if extracted:
        inferred_graph = infer_cross_entity_links(
            extracted, g, ontology, console=console,
        )
        for triple in inferred_graph:
            g.add(triple)

    # ── TEMPLATES PHASE — SPARQL recognition + batched-loop LLM-confirm.
    # Returns a dedicated graph that's serialized separately (so reviewers
    # can see what the template phase asserted vs the mega-walker's
    # binary-property output). The LLM-confirm sub-phase also mutates *g*
    # in place to add NEW lowered triples (so iteration N+1's SPARQL can
    # see iteration N's confirmations); those triples appear in BOTH the
    # extract graph (for the union view) AND the templates graph (for
    # the dedicated debug file).
    g_templates = Graph()
    if extracted:
        console.print("[bold]templates[/bold]")
        g_templates = walk_templates(
            g, extracted=extracted, ontology=ontology, base_ns=base_ns,
            markdown=full_markdown, client=client, model=model, console=console,
        )

    # ── Form classification deferred ──
    # Lands once at least one user-ingested form ontology is loaded.

    # ── Serialize each layer to its own named-graph file ──
    convert_file   = g_dir / f"{slug}.convert{GRAPH_SUFFIX}"
    extract_file   = g_dir / f"{slug}.extract{GRAPH_SUFFIX}"
    templates_file = g_dir / f"{slug}.templates{GRAPH_SUFFIX}"
    g_convert.serialize(destination=str(convert_file), format="turtle")
    g.serialize(destination=str(extract_file), format="turtle")
    parts = [
        f"[dim]{slug}.convert{GRAPH_SUFFIX}[/dim] ({len(g_convert)} triples)",
        f"[dim]{slug}.extract{GRAPH_SUFFIX}[/dim] ({len(g)} triples)",
    ]
    if len(g_templates) > 0:
        g_templates.serialize(destination=str(templates_file), format="turtle")
        parts.append(f"[dim]{slug}.templates{GRAPH_SUFFIX}[/dim] ({len(g_templates)} triples)")
    console.print(f"  wrote   {' + '.join(parts)}")

    # Register the source pointing at the convert file (the always-present
    # layer); the loader picks up all `<slug>.*.ttl` siblings via glob, so
    # extract / enrich / future stage files compose automatically.
    _register_source(
        project_root, slug, source, convert_file,
        file_hash=file_hash, file_size=file_size, mime_type="application/pdf",
    )
    console.print(f"  registered as [bold]{slug}[/bold]")
    return convert_file


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _build_document_context(*, title: str, description: str, markdown: str) -> str:
    """Build the small "document context" block injected into stage 2 prompts.

    Property extraction sees only an entity's supporting quotes (cheap,
    targeted), but some properties (issue date, sender, document number)
    typically live in headers far from any specific entity's quotes. This
    block carries the stable header info so stage 2 isn't blind to it.
    """
    parts = [f"Title: {title!r}"]
    if description:
        parts.append(f"Description: {description}")
    head = (markdown or "").strip()[:600]
    if head:
        parts.append(f"Document head (first ~600 chars):\n{head}")
    return "\n".join(parts)
