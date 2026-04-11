"""Persist classification results as RDF, reusing the existing tax: ontology."""

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from rdflib import BNode, Graph, Literal as RDFLiteral, Namespace, RDF, URIRef
from rdflib.namespace import SKOS, XSD

from .models import DocumentHit, ModelConfig
from .ontology import JSONLD_CONTEXT

logger = logging.getLogger(__name__)

TAX  = Namespace("http://example.org/tax-classifier/")
FIN  = Namespace("http://example.org/financial/")
LLM  = Namespace("http://example.org/llm#")
FS   = Namespace("http://example.org/fs#")
PROV = Namespace("http://www.w3.org/ns/prov#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")

_AGENT_TYPES = {FOAF.Person, FOAF.Organization, FOAF.Agent}


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-_.]", "-", value)


def _normalize_name(name: str) -> str:
    """Normalize a display name to a stable token set for URI minting.
    'Foo / Bar' and 'Foo, Bar' both become 'bar_foo'.
    """
    tokens = re.split(r"[\s/,;|]+", name.lower())
    return "_".join(sorted(t for t in tokens if t))


def _agent_uri(name: str) -> URIRef:
    """Mint a stable URI for any foaf:Agent from a display name."""
    return TAX[f"party_{_normalize_name(name)}"]


def _doc_uri(pdf_path: Path, category: str) -> URIRef:
    return TAX[f"doc_{_safe(pdf_path.stem)}_{_safe(category)}"]


def doc_uri_for_pdf(pdf_path: Path):
    """Return a callable() → URI string for use as run_extraction's doc_uri_for.

    The URI includes only the filename stem (category is not known yet at call time).
    It is later updated in the results graph to include the category once the agent
    has classified the document.
    """
    def _factory() -> str:
        return str(TAX[f"doc_{_safe(pdf_path.stem)}"])
    return _factory


def _activity_uri(pdf_path: Path, category: str) -> URIRef:
    return TAX[f"activity_{_safe(pdf_path.stem)}_{_safe(category)}"]


def _file_uri(pdf_path: Path) -> URIRef:
    return FS[_safe(pdf_path.stem)]


def _file_metadata(pdf_path: Path) -> tuple[str, str, str]:
    """Return (sha256_hex, created_iso, modified_iso) for a PDF."""
    sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    stat = pdf_path.stat()
    modified = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()
    created  = datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat()
    return sha256, created, modified


def pdf_sha256(pdf_path: Path) -> str:
    return hashlib.sha256(pdf_path.read_bytes()).hexdigest()


def find_classified(results_path: Path, sha256: str) -> URIRef | None:
    """Return the doc URI if a file with this SHA-256 is already in results_path."""
    if not results_path.exists():
        return None
    g = Graph()
    g.parse(results_path)
    for file_node in g.subjects(FS.sha256, RDFLiteral(sha256)):
        for doc_node in g.subjects(PROV.wasDerivedFrom, file_node):
            return doc_node  # type: ignore[return-value]
    return None


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


def _mint_agent_uris(g: Graph) -> Graph:
    """Replace blank nodes typed as foaf:Agent subclasses with stable URIs."""
    bn_to_uri: dict[BNode, URIRef] = {}
    for node in set(g.subjects()):
        if not isinstance(node, BNode):
            continue
        for rdf_type in g.objects(node, RDF.type):
            if rdf_type in _AGENT_TYPES:
                name = g.value(node, FOAF.name)
                if name:
                    bn_to_uri[node] = _agent_uri(str(name))
                break

    if not bn_to_uri:
        return g

    new_g = Graph()
    for s, p, o in g:
        s = bn_to_uri.get(s, s)
        o = bn_to_uri.get(o, o) if isinstance(o, BNode) else o
        new_g.add((s, p, o))
    return new_g


def _remove_doc_subgraph(g: Graph, doc_node: URIRef) -> None:
    """Remove doc_node triples and all blank nodes transitively reachable from it."""
    visited: set = set()
    queue: list = [doc_node]
    while queue:
        node = queue.pop()
        if node in visited:
            continue
        visited.add(node)
        for s, p, o in list(g.triples((node, None, None))):
            g.remove((s, p, o))
            if isinstance(o, BNode):
                queue.append(o)


def append_result(
    results_path: Path,
    pdf_path: Path,
    hit: DocumentHit,
    model: ModelConfig,
    method: Literal["text", "vision", "markdown", "agent"],
    doc_class_uri: URIRef,
) -> None:
    """
    Add (or overwrite) a classification result for one detected document type.
    Document details are stored as JSON-LD from the LLM; provenance triples
    are added by this function.
    """
    g = _load_or_create(results_path)
    now = datetime.now(timezone.utc).isoformat()

    file_node     = _file_uri(pdf_path)
    activity_node = _activity_uri(pdf_path, hit.category)
    doc_node      = _doc_uri(pdf_path, hit.category)
    agent_node    = model.uri

    # Remove existing triples for these nodes (re-classification)
    g.remove((file_node,     None, None))
    g.remove((activity_node, None, None))
    _remove_doc_subgraph(g, doc_node)

    # ── Parse and merge JSON-LD from LLM ──────────────────────────────────────
    if hit.details:
        jsonld_data = dict(hit.details)
        if "@context" not in jsonld_data:
            jsonld_data["@context"] = JSONLD_CONTEXT

        temp_g = Graph()
        try:
            temp_g.parse(data=json.dumps(jsonld_data, ensure_ascii=False), format="json-ld")
            temp_g = _mint_agent_uris(temp_g)

            root_node = next(
                (s for s in temp_g.subjects(RDF.type, doc_class_uri)),
                None,
            )
            for s, p, o in temp_g:
                ns = doc_node if s == root_node else s
                no = doc_node if isinstance(o, BNode) and o == root_node else o
                g.add((ns, p, no))
        except Exception as exc:
            logger.warning("Failed to parse JSON-LD details for %s: %s", hit.category, exc)

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

    # ── Document provenance ───────────────────────────────────────────────────
    # These are added after JSON-LD merge so they always win over LLM output.
    g.add((doc_node, RDF.type,             doc_class_uri))
    g.add((doc_node, RDF.type,             PROV.Entity))
    g.add((doc_node, PROV.wasGeneratedBy,  activity_node))
    g.add((doc_node, PROV.wasDerivedFrom,  file_node))
    g.add((doc_node, PROV.wasAttributedTo, agent_node))

    results_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(results_path), format="turtle")
