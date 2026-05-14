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

DG  = Namespace("http://example.org/docgraph/meta#")
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
    """Build the file → document chain. No chapters, no quotes.

    Returns an rdflib Graph with the structural triples plus PROV-O for the
    convert step. Subject classification triples are added by the caller
    after this function returns.
    """
    g = Graph()
    _bind_prefixes(g, file_uri)

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
        if title := pdf_info.get("Title"):
            g.add((file_uri, RDFS.label, Literal(title)))

    # Document
    g.add((doc_uri, RDF.type, DG.Document))
    g.add((doc_uri, RDF.type, LIS.InformationObject))
    g.add((doc_uri, RDFS.label, Literal(document_title)))
    if document_description:
        g.add((doc_uri, RDFS.comment, Literal(document_description)))
    g.add((file_uri, LIS.representedBy, doc_uri))

    # Source-text file: a representation of the document derived from the
    # original PDF. Either an HTML file (canonical, current pipeline) or a
    # Markdown file (legacy). Fragment URIs in the extract graph anchor
    # into this file via standard URL fragments (`<file#id-N>`).
    if md_uri is not None and md_file_path is not None:
        suffix = md_file_path.suffix.lower()
        is_html = suffix in (".html", ".htm")
        g.add((md_uri, RDF.type, DG.HtmlFile if is_html else DG.MarkdownFile))
        g.add((md_uri, RDF.type, LIS.PhysicalObject))
        g.add((md_uri, RDF.type, PROV.Entity))
        g.add((md_uri, DG.filePath, Literal(str(md_file_path.relative_to(project_root)))))
        g.add((md_uri, DG.mimeType, Literal("text/html" if is_html else "text/markdown")))
        g.add((md_uri, LIS.representedBy, doc_uri))
        g.add((md_uri, PROV.wasDerivedFrom, file_uri))

    # Conversion activity
    if convert_started and convert_ended:
        from rdflib import Namespace as _NS
        base_ns = _NS(str(file_uri) + "/")
        conv_uri = URIRef(base_ns["convert"])
        g.add((conv_uri, RDF.type, PROV.Activity))
        g.add((conv_uri, RDFS.label, Literal("PDF → Markdown conversion")))
        g.add((conv_uri, PROV.startedAtTime,
               Literal(convert_started.isoformat(), datatype=XSD.dateTime)))
        g.add((conv_uri, PROV.endedAtTime,
               Literal(convert_ended.isoformat(), datatype=XSD.dateTime)))
        g.add((conv_uri, PROV.used, file_uri))
        g.add((conv_uri, PROV.generated, doc_uri))
        if convert_agent_uri:
            g.add((conv_uri, PROV.wasAssociatedWith, convert_agent_uri))

    return g


def _bind_prefixes(g: Graph, file_uri: URIRef) -> None:
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("prov", PROV, override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   Namespace(str(file_uri) + "/"), override=True, replace=True)
