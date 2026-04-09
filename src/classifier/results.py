"""Persist classification results as RDF, reusing the existing tax: ontology."""

import hashlib
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from rdflib import Graph, Literal as RDFLiteral, Namespace, RDF, URIRef
from rdflib.namespace import SKOS, XSD

from .models import ClassificationResult, DocumentClass, DocumentHit, ModelConfig, PropertyDef

TAX  = Namespace("http://example.org/tax-classifier/")
FIN  = Namespace("http://example.org/financial/")
LLM  = Namespace("http://example.org/llm#")
FS   = Namespace("http://example.org/fs#")
PROV = Namespace("http://www.w3.org/ns/prov#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-_.]", "-", value)


def _doc_uri(pdf_path: Path, category: str) -> URIRef:
    return TAX[f"doc_{_safe(pdf_path.stem)}_{_safe(category)}"]


def _activity_uri(pdf_path: Path, category: str) -> URIRef:
    return TAX[f"activity_{_safe(pdf_path.stem)}_{_safe(category)}"]


def _file_uri(pdf_path: Path) -> URIRef:
    return FS[_safe(pdf_path.stem)]


def _parse_decimal(value: object) -> Decimal:
    """
    Parse a money/numeric string to Decimal.
    Strips currency symbols and codes (€, $, EUR, USD, …) then normalises
    locale-specific separators:
      European: "1.234,56" or "EUR 115,84" → 1234.56 / 115.84
      US/ISO:   "1,234.56" or "$1,500.00"  → 1234.56 / 1500.00
    """
    # Keep only digits, comma, dot, minus
    s = re.sub(r"[^\d,.\-]", "", str(value))
    # European format: trailing comma + ≤2 digits → comma is decimal sep, dots are thousands
    if re.search(r",\d{1,2}$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")  # US/ISO: comma is thousands separator
    return Decimal(s)


def _parse_date(value: object) -> date:
    """Parse an ISO date string (YYYY-MM-DD) to datetime.date."""
    return date.fromisoformat(str(value).strip())


def _parse_monetary(value: object) -> tuple[Decimal, str]:
    """
    Split a string like "115.84 EUR" or "EUR 1,234.56" into (Decimal, currency_code).
    Currency defaults to empty string when no 3-letter ISO code is found.
    """
    s = str(value)
    m = re.search(r"\b([A-Z]{3})\b", s)
    currency = m.group(1) if m else ""
    amount = _parse_decimal(s)
    return amount, currency


def _file_metadata(pdf_path: Path) -> tuple[str, str, str]:
    """
    Return (sha256_hex, created_iso, modified_iso) for a PDF.
    Note: on Linux st_ctime is inode-change time, not birth time.
    """
    sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    stat = pdf_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    created  = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
    return sha256, created, modified


def pdf_sha256(pdf_path: Path) -> str:
    """Return the SHA-256 hex digest of a PDF file."""
    return hashlib.sha256(pdf_path.read_bytes()).hexdigest()


def find_classified(results_path: Path, sha256: str) -> URIRef | None:
    """
    Return the doc URI if a file with this SHA-256 checksum is already in
    results_path, or None if not found.
    """
    if not results_path.exists():
        return None
    g = Graph()
    g.parse(results_path)
    for file_node in g.subjects(FS.sha256, RDFLiteral(sha256)):
        for doc_node in g.subjects(PROV.wasDerivedFrom, file_node):
            return doc_node  # type: ignore[return-value]
    return None


def _monetary_uri(doc_node: URIRef, field_key: str) -> URIRef:
    """Mint a stable URI for a tax:MonetaryAmount scoped to a document node."""
    stem = str(doc_node).rsplit("/", 1)[-1]
    return TAX[f"{stem}_{field_key}"]


def _coerce(value: object, rdf_range: URIRef) -> RDFLiteral:
    """Cast a Python value to a typed RDF literal."""
    if rdf_range in (XSD.decimal, XSD.float, XSD.double):
        try:
            return RDFLiteral(_parse_decimal(value), datatype=XSD.decimal)
        except (InvalidOperation, ValueError):
            pass
    if rdf_range == XSD.date:
        try:
            return RDFLiteral(_parse_date(value), datatype=XSD.date)
        except ValueError:
            pass
    if rdf_range == XSD.gYear:
        try:
            return RDFLiteral(int(str(value).strip()), datatype=XSD.gYear)
        except ValueError:
            pass
    if rdf_range == XSD.boolean:
        if isinstance(value, str):
            value = value.lower() in ("true", "yes", "1")
        return RDFLiteral(bool(value), datatype=XSD.boolean)
    return RDFLiteral(str(value))


def _load_or_create(results_path: Path) -> Graph:
    g = Graph()
    g.bind("tax",  TAX)
    g.bind("fin",  FIN)
    g.bind("llm",  LLM)
    g.bind("fs",   FS)
    g.bind("prov", PROV)
    g.bind("foaf", FOAF)
    g.bind("skos", SKOS)
    g.bind("xsd",  XSD)
    if results_path.exists():
        g.parse(results_path)
    return g


def _agent_uri(name: str, foaf_type: URIRef) -> URIRef:
    """Mint a stable URI for a foaf:Person or foaf:Organization from a display name."""
    prefix = "person" if foaf_type == FOAF.Person else "org"
    return TAX[f"{prefix}_{_safe(name)}"]


def append_result(
    results_path: Path,
    pdf_path: Path,
    hit: DocumentHit,
    result: ClassificationResult,
    model: ModelConfig,
    method: Literal["text", "vision"],
    doc_class: DocumentClass,
    class_props: list[PropertyDef],
) -> None:
    """
    Add (or overwrite) a classification result for one detected document type.

    The document node is typed as the specific OWL class (e.g. fin:Bill)
    and carries typed property assertions derived from the extracted details.
    Provenance (who classified, how, when) lives on the activity node.
    Each detected type in the same PDF gets its own doc + activity node,
    scoped by category to avoid collision.
    """
    g = _load_or_create(results_path)
    now = datetime.now(timezone.utc).isoformat()

    file_node     = _file_uri(pdf_path)
    activity_node = _activity_uri(pdf_path, hit.category)
    doc_node      = _doc_uri(pdf_path, hit.category)
    agent_node    = model.uri

    # Remove all previous triples for these nodes (re-classification).
    # Also collect FOAF/MonetaryAmount nodes referenced by the doc node so we
    # can remove their triples if nothing else in the graph points to them.
    _SCOPED_TYPES = {FOAF.Person, FOAF.Organization, FOAF.Agent, TAX.MonetaryAmount, FIN.LineItem}
    scoped_nodes: set[URIRef] = set()
    for _, _, obj in g.triples((doc_node, None, None)):
        if isinstance(obj, URIRef):
            if any(g.triples((obj, RDF.type, t)) for t in _SCOPED_TYPES):
                scoped_nodes.add(obj)

    g.remove((file_node,     None, None))
    g.remove((activity_node, None, None))
    g.remove((doc_node,      None, None))

    # Remove scoped nodes that are now unreferenced
    for node in scoped_nodes:
        if not any(True for _ in g.subjects(None, node)):
            g.remove((node, None, None))

    # ── Source file (fs:) ─────────────────────────────────────────────────────
    sha256, created, modified = _file_metadata(pdf_path)
    g.add((file_node, RDF.type,        FS.File))
    g.add((file_node, RDF.type,        PROV.Entity))
    g.add((file_node, FS.fileName,     RDFLiteral(pdf_path.name)))
    g.add((file_node, FS.filePath,     RDFLiteral(str(pdf_path.resolve()))))
    g.add((file_node, FS.sha256,       RDFLiteral(sha256)))
    g.add((file_node, FS.createdAt,    RDFLiteral(created,  datatype=XSD.dateTime)))
    g.add((file_node, FS.modifiedAt,   RDFLiteral(modified, datatype=XSD.dateTime)))

    # ── Classification activity (llm:) ────────────────────────────────────────
    g.add((activity_node, RDF.type,                  PROV.Activity))
    g.add((activity_node, RDF.type,                  LLM.ClassificationActivity))
    g.add((activity_node, PROV.wasAssociatedWith,    agent_node))
    g.add((activity_node, PROV.used,                 file_node))
    g.add((activity_node, PROV.endedAtTime,          RDFLiteral(now, datatype=XSD.dateTime)))
    g.add((activity_node, LLM.classificationMethod,  RDFLiteral(method)))
    g.add((activity_node, TAX.confidence,            RDFLiteral(
        round(hit.confidence, 4), datatype=XSD.decimal
    )))
    g.add((activity_node, TAX.reason,                RDFLiteral(hit.reason)))

    # ── Document instance typed as the specific OWL class ────────────────────
    g.add((doc_node, RDF.type,             doc_class.uri))
    g.add((doc_node, RDF.type,             PROV.Entity))
    g.add((doc_node, PROV.wasGeneratedBy,  activity_node))
    g.add((doc_node, PROV.wasDerivedFrom,  file_node))
    g.add((doc_node, PROV.wasAttributedTo, agent_node))

    # ── Typed property assertions from extracted details ──────────────────────
    if hit.details:
        prop_by_key = {p.field_key: p for p in class_props}
        doc_stem = str(doc_node).rsplit("/", 1)[-1]
        for key, value in hit.details.items():
            if value is None:
                continue
            prop = prop_by_key.get(key)
            if not prop:
                continue
            if prop.is_compound_list and isinstance(value, list):
                sub_by_key = {s.field_key: s for s in prop.item_schema}
                for i, item_dict in enumerate(value):
                    if not isinstance(item_dict, dict):
                        continue
                    item_node = TAX[f"{doc_stem}_{prop.field_key}_{i}"]
                    g.add((item_node, RDF.type,   prop.rdf_range))
                    g.add((doc_node,  prop.uri,   item_node))
                    for item_key, item_val in item_dict.items():
                        if item_val is None:
                            continue
                        sub = sub_by_key.get(item_key)
                        if not sub:
                            continue
                        g.add((item_node, sub.uri, _coerce(item_val, sub.rdf_range)))
            elif prop.is_compound_object and isinstance(value, dict):
                name = str(value.get("name") or prop.field_key)
                party_node = _agent_uri(name, prop.rdf_range)
                g.add((party_node, RDF.type,  prop.rdf_range))
                g.add((party_node, FOAF.name, RDFLiteral(name)))
                g.add((doc_node, prop.uri, party_node))
                sub_by_key = {s.field_key: s for s in prop.item_schema}
                for item_key, item_val in value.items():
                    if item_val is None or item_key == "name":
                        continue
                    sub = sub_by_key.get(item_key)
                    if not sub:
                        continue
                    g.add((party_node, sub.uri, _coerce(item_val, sub.rdf_range)))
            elif prop.is_monetary:
                amount, currency = _parse_monetary(value)
                m_node = _monetary_uri(doc_node, prop.field_key)
                g.add((m_node, RDF.type,          TAX.MonetaryAmount))
                g.add((m_node, TAX.numericValue,  RDFLiteral(amount, datatype=XSD.decimal)))
                g.add((m_node, TAX.currency,      RDFLiteral(currency)))
                g.add((doc_node, prop.uri, m_node))
            elif prop.is_object_property:
                party_node = _agent_uri(str(value), prop.rdf_range)
                g.add((party_node, RDF.type,  prop.rdf_range))
                g.add((party_node, FOAF.name, RDFLiteral(str(value))))
                g.add((doc_node, prop.uri, party_node))
            else:
                g.add((doc_node, prop.uri, _coerce(value, prop.rdf_range)))

    results_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(results_path), format="turtle")
