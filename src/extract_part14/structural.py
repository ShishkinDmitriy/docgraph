"""Build the file → document chain in Part 14 idiom.

M1 produces only:

    <file>     a dg:PdfFile, lis:PhysicalObject, prov:Entity ;
               dg:filePath/Hash/Size/MimeType/PageCount ... ;
               lis:representedBy <doc> .

    <doc>      a dg:Document, lis:InformationObject ;
               rdfs:label "<title>" .

No chapters, no quotes — those are minted top-down by M2's branch walker as
evidence cited by extracted entities (see docs/architecture/extraction.md
§ Quote model). The `parse_markdown` helper stays because M2 uses it to give
the LLM structured markdown context.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import PROV, RDF, RDFS, XSD

DG  = Namespace("urn:docgraph:vocab:meta#")
LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")


# ── parse_markdown — unused in M1, used by M2 for structured LLM context ────

@dataclass
class QuoteSpec:
    text: str
    locator: str        # human-readable provenance ("Section 2 / ¶3")

    @property
    def uri_local(self) -> str:
        return "quote-" + hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:12]


@dataclass
class ChapterSpec:
    title: str
    quotes: list[QuoteSpec] = field(default_factory=list)


def parse_markdown(md_text: str) -> list[ChapterSpec]:
    """Split markdown into chapters (## headings) with paragraphs as quotes.

    NOT used in M1 (no bottom-up quote minting). M2's stage-1 entity
    extraction uses this to present structured markdown to the LLM so its
    quote citations can reference paragraph positions accurately. Filters
    structural-but-empty lines (separators, bare headings).
    """
    chapters: list[ChapterSpec] = []
    current = ChapterSpec(title="(prelude)")
    para_lines: list[str] = []

    def flush_paragraph():
        text = "\n".join(para_lines).strip()
        if text and not _is_structural_only(text):
            locator = f"{current.title} / ¶{len(current.quotes) + 1}"
            current.quotes.append(QuoteSpec(text=text, locator=locator))
        para_lines.clear()

    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## ") and not stripped.startswith("### "):
            flush_paragraph()
            if current.quotes or current.title != "(prelude)":
                chapters.append(current)
            current = ChapterSpec(title=stripped[3:].strip())
        elif stripped == "":
            flush_paragraph()
        else:
            para_lines.append(line)

    flush_paragraph()
    if current.quotes or current.title != "(prelude)":
        chapters.append(current)

    return chapters


def _is_structural_only(text: str) -> bool:
    """True if the paragraph is just markdown structure with no semantic content
    (horizontal rules, bare headings, repeated separators)."""
    stripped = text.strip()
    if not stripped:
        return True
    # Horizontal rules / separators
    if all(c in "-=*_" for c in stripped):
        return True
    # Bare heading (single line starting with #)
    lines = [l for l in stripped.splitlines() if l.strip()]
    if len(lines) == 1 and lines[0].lstrip().startswith("#"):
        return True
    return False


# ── build_chain — M1's structural emission (file → doc only) ────────────────

DCTERMS = Namespace("http://purl.org/dc/terms/")


def build_recognize_graph(
    file_path: Path,
    file_uri: URIRef,
    doc_uri: URIRef,
    *,
    project_root: Path,
    file_hash: str,
    file_size: int,
    mime_type: str,
    pdf_info: dict | None = None,
) -> Graph:
    """Recognize step (seq 1): typed file object + typed document object.

    Builds two entities — no HTML, no conversion activity yet (that's the
    convert step's delta):
      - <file_uri> a dg:PdfFile, lis:PhysicalObject, prov:Entity ; …
        carries pdfinfo-derived metadata (pageCount, pdfProducer)
      - <doc_uri> a dg:Document, lis:InformationObject ; …
        carries dcterms:title/creator/created/modified from pdfinfo when
        present, plus rdfs:label fallback. Linked back to the file via
        `<file> lis:representedBy <doc>`.

    Title/creator from pdfinfo are often noisy; the convert step may
    later REMOVE these and ADD better LLM-derived values via a follow-up
    delta. That's the delta system working as designed.
    """
    g = Graph()
    _bind_prefixes(g, file_uri)
    g.bind("dcterms", DCTERMS, override=True, replace=True)

    # File
    g.add((file_uri, RDF.type, DG.PdfFile))
    g.add((file_uri, RDF.type, LIS.PhysicalObject))
    g.add((file_uri, RDF.type, PROV.Entity))
    g.add((file_uri, DG.filePath, Literal(str(file_path.relative_to(project_root)))))
    g.add((file_uri, DG.fileHash, Literal(file_hash)))
    g.add((file_uri, DG.fileSize, Literal(file_size, datatype=XSD.integer)))
    g.add((file_uri, DG.mimeType, Literal(mime_type)))

    if pdf_info:
        if pages := pdf_info.get("Pages"):
            try:
                g.add((file_uri, DG.pageCount, Literal(int(pages), datatype=XSD.integer)))
            except (TypeError, ValueError):
                pass
        if producer := pdf_info.get("Producer"):
            g.add((file_uri, DG.pdfProducer, Literal(producer)))

    # Document
    g.add((doc_uri, RDF.type, DG.Document))
    g.add((doc_uri, RDF.type, LIS.InformationObject))
    g.add((file_uri, LIS.representedBy, doc_uri))

    if pdf_info:
        # Doc-level metadata via dcterms — title/creator/dates from pdfinfo.
        if title := pdf_info.get("Title"):
            g.add((doc_uri, DCTERMS.title, Literal(title)))
            g.add((doc_uri, RDFS.label,    Literal(title)))
        if author := pdf_info.get("Author"):
            g.add((doc_uri, DCTERMS.creator, Literal(author)))
        if created := pdf_info.get("CreationDate"):
            g.add((doc_uri, DCTERMS.created, Literal(created)))
        if modified := pdf_info.get("ModDate"):
            g.add((doc_uri, DCTERMS.modified, Literal(modified)))

    return g


def build_convert_graph(
    file_uri:           URIRef,
    doc_uri:            URIRef,
    html_uri:           URIRef,
    html_file_path:     Path,
    *,
    md_uri:             URIRef | None = None,
    md_file_path:       Path | None  = None,
    project_root:       Path,
    document_title:     str | None = None,
    document_description: str = "",
    convert_started:    datetime | None = None,
    convert_ended:      datetime | None = None,
    convert_agent_uri:  URIRef | None = None,
) -> Graph:
    """Convert step (seq 2): HTML file + markdown view + conversion activity.

    Adds:
      - <html_uri>  a dg:HtmlFile, lis:PhysicalObject, prov:Entity —
        the canonical converted view, lis:representedBy <doc>,
        prov:wasDerivedFrom <file>.
      - <md_uri>    a dg:MarkdownFile, lis:PhysicalObject, prov:Entity —
        OPTIONAL. The markdown projection of the HTML that's fed to
        the LLM as the extract prompt. prov:wasDerivedFrom <html>.
        Registering this gives the LLM-prompt input a stable URI for
        provenance + makes it discoverable as a regenerable artifact.
      - <conv>      a prov:Activity — the conversion run; prov:used
        <file>, prov:generated <html>, prov:wasAssociatedWith <agent>.

    Document gets a (likely better) rdfs:label / rdfs:comment from the
    LLM's HTML conversion title/description. Recognize's pdfinfo-based
    label stays — consumers picking the "newest" label win.
    """
    g = Graph()
    _bind_prefixes(g, file_uri)
    g.bind("dcterms", DCTERMS, override=True, replace=True)

    # HtmlFile — canonical converted view
    g.add((html_uri, RDF.type, DG.HtmlFile))
    g.add((html_uri, RDF.type, LIS.PhysicalObject))
    g.add((html_uri, RDF.type, PROV.Entity))
    g.add((html_uri, DG.filePath, Literal(str(html_file_path.relative_to(project_root)))))
    g.add((html_uri, DG.mimeType, Literal("text/html")))
    g.add((html_uri, LIS.representedBy, doc_uri))
    g.add((html_uri, PROV.wasDerivedFrom, file_uri))

    # MarkdownFile — LLM-prompt view derived from HTML
    if md_uri is not None and md_file_path is not None:
        g.add((md_uri, RDF.type, DG.MarkdownFile))
        g.add((md_uri, RDF.type, LIS.PhysicalObject))
        g.add((md_uri, RDF.type, PROV.Entity))
        g.add((md_uri, DG.filePath, Literal(str(md_file_path.relative_to(project_root)))))
        g.add((md_uri, DG.mimeType, Literal("text/markdown")))
        g.add((md_uri, LIS.representedBy, doc_uri))
        g.add((md_uri, PROV.wasDerivedFrom, html_uri))

    # Enrich the document with the LLM-derived title/description.
    if document_title:
        g.add((doc_uri, RDFS.label, Literal(document_title)))
    if document_description:
        g.add((doc_uri, RDFS.comment, Literal(document_description)))

    # Conversion activity (PDF → HTML)
    if convert_started and convert_ended:
        from rdflib import Namespace as _NS
        base_ns = _NS(str(file_uri) + "/")
        conv_uri = URIRef(base_ns["convert"])
        g.add((conv_uri, RDF.type, PROV.Activity))
        g.add((conv_uri, RDFS.label, Literal("PDF → HTML conversion")))
        g.add((conv_uri, PROV.startedAtTime,
               Literal(convert_started.isoformat(), datatype=XSD.dateTime)))
        g.add((conv_uri, PROV.endedAtTime,
               Literal(convert_ended.isoformat(), datatype=XSD.dateTime)))
        g.add((conv_uri, PROV.used, file_uri))
        g.add((conv_uri, PROV.generated, html_uri))
        if convert_agent_uri:
            g.add((conv_uri, PROV.wasAssociatedWith, convert_agent_uri))

    return g


# Backward-compat: kept for any callers still using the legacy single-call
# shape. New code should call build_recognize_graph + build_convert_graph
# separately and emit each as its own delta.
def build_chain(
    file_path: Path,
    file_uri: URIRef,
    doc_uri: URIRef,
    document_title: str,
    document_description: str,
    *,
    project_root: Path,
    file_hash: str,
    file_size: int,
    mime_type: str,
    md_uri: URIRef | None = None,
    md_file_path: Path | None = None,
    pdf_info: dict | None = None,
    convert_started: datetime | None = None,
    convert_ended: datetime | None = None,
    convert_agent_uri: URIRef | None = None,
) -> Graph:
    """Legacy combined builder — recognize triples + convert triples in one Graph."""
    g = build_recognize_graph(
        file_path=file_path, file_uri=file_uri, doc_uri=doc_uri,
        project_root=project_root,
        file_hash=file_hash, file_size=file_size, mime_type=mime_type,
        pdf_info=pdf_info,
    )
    if md_uri is not None and md_file_path is not None:
        # Legacy callers passed the HTML file via `md_uri` (back when
        # this was misnamed); the back-compat shim treats it as the
        # HTML URI/path. Markdown view is unset in this legacy shape.
        for triple in build_convert_graph(
            file_uri=file_uri, doc_uri=doc_uri,
            html_uri=md_uri, html_file_path=md_file_path,
            project_root=project_root,
            document_title=document_title, document_description=document_description,
            convert_started=convert_started, convert_ended=convert_ended,
            convert_agent_uri=convert_agent_uri,
        ):
            g.add(triple)
    return g


def _bind_prefixes(g: Graph, file_uri: URIRef) -> None:
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("prov", PROV, override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   Namespace(str(file_uri) + "/"), override=True, replace=True)
