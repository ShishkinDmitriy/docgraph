"""Build the file → document chain in Part 14 idiom.

Two builders, one per pipeline step:

  build_recognize_graph(...)  — seq 1: typed file + document + metadata
  build_convert_graph(...)    — seq 2: HtmlFile + MarkdownFile + activity

Each emits the named graphs the pipeline writes as its own delta.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import PROV, RDF, RDFS, XSD

DG      = Namespace("urn:docgraph:vocab:meta#")
LIS     = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")
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

    Each metadata field on the file is emitted as a uniform LIS-14
    Quality + Datum chain, so the templates step folds every chain into
    one tpl:Invocation — `lis14tpl:PhysicalObjectHasQuantity` for the
    scalar size, `lis14tpl:ObjectHasNominalQuality` for the nominal
    path/hash/mimeType/createdBy. The lifted invocations replace the
    lowered triples in the final doc graph.

    File ⇄ Document direction follows LIS-14: the bytes ARE a
    representation of the abstract document content, so:
        <file> lis:represents <doc>
    (range-free; the file plays the representor role). The inverse
    `<doc> lis:representedBy <file>` would require typing the file as
    InformationObject, which it is not in our model.

    Page count attaches to the *document* — pagination is intrinsic to
    the work, not to the bytes. Title/creator/dates from pdfinfo land
    on the document as dcterms metadata; the convert step may later
    overwrite these via a follow-up delta.
    """
    g = Graph()
    _bind_prefixes(g, file_uri)
    g.bind("dcterms", DCTERMS, override=True, replace=True)

    # File — typing only; every metadata field becomes a Quality chain.
    g.add((file_uri, RDF.type, DG.PdfFile))
    g.add((file_uri, RDF.type, LIS.PhysicalObject))
    g.add((file_uri, RDF.type, PROV.Entity))

    # Scalar quality: size in bytes (LT_0003 PhysicalObjectHasQuantity).
    _emit_scalar_quality(
        g, bearer=file_uri, quality_local="size",
        quality_type=DG.FileSize, value=float(file_size), uom=DG.Byte,
    )
    # Nominal qualities: path, hash, mimeType (LT_nominal).
    _emit_nominal_quality(
        g, bearer=file_uri, quality_local="path",
        quality_type=DG.FilePath,
        value=str(file_path.relative_to(project_root)),
    )
    _emit_nominal_quality(
        g, bearer=file_uri, quality_local="hash",
        quality_type=DG.FileHash, value=file_hash,
    )
    _emit_nominal_quality(
        g, bearer=file_uri, quality_local="mime",
        quality_type=DG.MimeType, value=mime_type,
    )

    # Created-by quality: prefer pdfinfo Author (the human), fall back to
    # Producer (the software) — both, when present, describe who/what
    # brought the file into being.
    creator_value = None
    if pdf_info:
        creator_value = pdf_info.get("Author") or pdf_info.get("Producer")
    if creator_value:
        _emit_nominal_quality(
            g, bearer=file_uri, quality_local="createdBy",
            quality_type=DG.CreationAgent, value=str(creator_value),
        )

    # Document — InformationObject side of the file→doc representation.
    g.add((doc_uri, RDF.type, DG.Document))
    g.add((doc_uri, RDF.type, LIS.InformationObject))
    # The file represents the document (bytes embody the abstract work).
    g.add((file_uri, LIS.represents, doc_uri))

    if pdf_info:
        # Pages live on the *document* (intrinsic to the paginated work),
        # not on the file's PhysicalObject. Use `lis:hasQuality` — the
        # general property — since `lis:hasPhysicalQuantity` is
        # restricted to PhysicalObject subjects.
        if pages_raw := pdf_info.get("Pages"):
            try:
                pages = int(pages_raw)
            except (TypeError, ValueError):
                pages = None
            if pages is not None:
                _emit_scalar_quality(
                    g, bearer=doc_uri, quality_local="pages",
                    quality_type=DG.PageCount, value=float(pages),
                    uom=DG.Page, bearer_property=LIS.hasQuality,
                )

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


def _emit_scalar_quality(
    g: Graph, *,
    bearer: URIRef,
    quality_local: str,
    quality_type: URIRef,
    value: float,
    uom: URIRef,
    bearer_property: URIRef = LIS.hasPhysicalQuantity,
) -> None:
    """Emit the scalar Quality+ScalarQuantityDatum chain (LT_0003):

        <bearer> hasPhysicalQuantity <quality> .
        <quality> a Quality, <quality_type> ;
                  qualityQuantifiedAs <datum> .
        <datum> a ScalarQuantityDatum ;
                datumUOM <uom> ; datumValue <value>^^xsd:double .

    For non-PhysicalObject bearers (InformationObject, …) pass
    `bearer_property=lis:hasQuality` — the more general superproperty.
    """
    quality_uri, datum_uri = _quality_and_datum_uris(bearer, quality_local)
    g.add((bearer,      bearer_property,            quality_uri))
    g.add((quality_uri, RDF.type,                   LIS.Quality))
    g.add((quality_uri, RDF.type,                   quality_type))
    g.add((quality_uri, LIS.qualityQuantifiedAs,    datum_uri))
    g.add((datum_uri,   RDF.type,                   LIS.ScalarQuantityDatum))
    g.add((datum_uri,   LIS.datumUOM,               uom))
    g.add((datum_uri,   LIS.datumValue,             Literal(value, datatype=XSD.double)))


def _emit_nominal_quality(
    g: Graph, *,
    bearer: URIRef,
    quality_local: str,
    quality_type: URIRef,
    value: str,
) -> None:
    """Emit the nominal Quality+QuantityDatum chain (no UoM, string value):

        <bearer> hasQuality <quality> .
        <quality> a Quality, <quality_type> ;
                  qualityQuantifiedAs <datum> .
        <datum> a QuantityDatum ; datumValue "<value>" .

    Matched by `lis14tpl:ObjectHasNominalQuality` and folded by the
    templates step into one invocation per quality.
    """
    quality_uri, datum_uri = _quality_and_datum_uris(bearer, quality_local)
    g.add((bearer,      LIS.hasQuality,             quality_uri))
    g.add((quality_uri, RDF.type,                   LIS.Quality))
    g.add((quality_uri, RDF.type,                   quality_type))
    g.add((quality_uri, LIS.qualityQuantifiedAs,    datum_uri))
    g.add((datum_uri,   RDF.type,                   LIS.QuantityDatum))
    g.add((datum_uri,   LIS.datumValue,             Literal(value)))


def _quality_and_datum_uris(bearer: URIRef, local: str) -> tuple[URIRef, URIRef]:
    """Mint `<bearer>/<local>` and `<bearer>/<local>-datum` for a chain."""
    return URIRef(f"{bearer}/{local}"), URIRef(f"{bearer}/{local}-datum")


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


def _bind_prefixes(g: Graph, file_uri: URIRef) -> None:
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("prov", PROV, override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   Namespace(str(file_uri) + "/"), override=True, replace=True)
