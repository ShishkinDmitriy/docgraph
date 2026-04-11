"""Load OWL/RDF ontology data: self-description, document classes, model config."""

import logging
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import OWL, RDFS, SKOS

from .models import DocumentClass, ModelConfig

logger = logging.getLogger(__name__)

TAX  = Namespace("http://example.org/tax-classifier/")
FIN  = Namespace("http://example.org/financial/")
LLM  = Namespace("http://example.org/llm#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")
PAPYRUS = Namespace("http://example.org/tax-classifier/papyrus#")

# Canonical @context used in JSON-LD extraction prompts and result parsing.
# Populated from papyrus.ttl at startup via load_papyrus(); the values below are
# only a fallback in case load_papyrus() is not called.
JSONLD_CONTEXT: dict[str, str] = {
    "fin":  "http://example.org/financial/",
    "tax":  "http://example.org/tax-classifier/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "xsd":  "http://www.w3.org/2001/XMLSchema#",
}

# Output namespace and prefix used for minting entity and document URIs.
# Set by load_papyrus() from papyrus:this → papyrus:results.
OUTPUT_NS:     Namespace = Namespace("")
OUTPUT_PREFIX: str       = ""


@dataclass
class PapyrusConfig:
    """Configuration derived from data/papyrus.ttl."""
    namespaces:   dict[str, str]  # prefix → namespace URI
    target_class: URIRef          # OWL class whose subclasses are classification targets
    graph:        Graph           # all local (+ optionally remote) ontologies merged
    output_path:  Path            # where results.ttl is written


def load_papyrus(papyrus_path: Path, *, load_remote: bool = False) -> PapyrusConfig:
    """
    Parse data/papyrus.ttl and return the project configuration.

    Loads every papyrus:LocalOntology listed via papyrus:hasOntology into a
    single combined graph.  If load_remote=True, also fetches
    papyrus:RemoteOntology URLs (skipped with a warning on network error).

    Also updates the module-level JSONLD_CONTEXT dict in-place.
    """
    papyrus_graph = Graph()
    papyrus_graph.parse(papyrus_path)
    project_root = papyrus_path.parent.parent  # data/papyrus.ttl → data/ → project/

    # ── Find the Papyrus individual ───────────────────────────────────────────
    self_individual = papyrus_graph.value(predicate=RDF.type, object=PAPYRUS.Self)
    if self_individual is None:
        raise ValueError(f"{papyrus_path}: no individual of type papyrus:Self found")

    # ── Namespace map — read directly from @prefix declarations ───────────────
    # g.namespaces() returns every prefix bound in the file (plus rdflib defaults).
    # We collect only the prefixes that are explicitly claimed by an ontology
    # instance via papyrus:prefix, so the result is intentional rather than implicit.
    declared_ns = dict(papyrus_graph.namespaces())  # prefix → Namespace URI
    namespaces: dict[str, str] = {}
    for ont in papyrus_graph.objects(self_individual, PAPYRUS.hasOntology):
        p = papyrus_graph.value(ont, PAPYRUS.prefix)
        if p and str(p) in declared_ns:
            namespaces[str(p)] = str(declared_ns[str(p)])

    JSONLD_CONTEXT.clear()
    JSONLD_CONTEXT.update(namespaces)

    # ── Target class ──────────────────────────────────────────────────────────
    target_class = papyrus_graph.value(self_individual, PAPYRUS.targetClass)
    if target_class is None:
        raise ValueError(f"{papyrus_path}: papyrus:this has no papyrus:targetClass")

    # ── Build combined ontology graph ─────────────────────────────────────────
    combined = Graph()
    combined += papyrus_graph  # papyrus.ttl itself is part of the graph

    for ont in papyrus_graph.objects(self_individual, PAPYRUS.hasOntology):
        ont_types = set(papyrus_graph.objects(ont, RDF.type))

        if PAPYRUS.LocalOntology in ont_types:
            rel = papyrus_graph.value(ont, PAPYRUS.relativePath)
            if rel is None:
                logger.warning("Local ontology <%s> has no papyrus:relativePath — skipped", ont)
                continue
            path = project_root / str(rel)
            logger.debug("Loading local ontology from %s", path)
            combined.parse(path)

        elif PAPYRUS.RemoteOntology in ont_types:
            url = papyrus_graph.value(ont, PAPYRUS.url)  # URIRef — set directly, no cast needed
            if not load_remote:
                logger.debug("Skipping remote ontology <%s> (pass load_remote=True to fetch)", url)
                continue
            if url is None:
                logger.warning("Remote ontology <%s> has no papyrus:url — skipped", ont)
                continue
            try:
                logger.debug("Fetching remote ontology from %s", url)
                combined.parse(str(url))
            except Exception as exc:
                logger.warning("Could not fetch remote ontology <%s>: %s", url, exc)

    # ── Validate papyrus:this against papyrus:PapyrusShape ────────────────────
    try:
        from pyshacl import validate as shacl_validate
        conforms, _, report_text = shacl_validate(
            combined,
            shacl_graph=combined,
            inference="none",
            abort_on_first=False,
        )
        if not conforms:
            raise ValueError(f"papyrus.ttl validation failed:\n{report_text}")
    except ImportError:
        logger.warning("pyshacl not installed — skipping papyrus:this validation")

    # ── Output config ─────────────────────────────────────────────────────────
    output_node = papyrus_graph.value(self_individual, PAPYRUS.output)
    if output_node is None:
        raise ValueError(f"{papyrus_path}: papyrus:this has no papyrus:output")
    output_rel = papyrus_graph.value(output_node, PAPYRUS.relativePath)
    output_ns  = papyrus_graph.value(output_node, PAPYRUS.namespace)
    if output_rel is None or output_ns is None:
        raise ValueError(f"{papyrus_path}: papyrus:output must have relativePath and namespace")

    output_prefix = papyrus_graph.value(output_node, PAPYRUS.prefix)

    global OUTPUT_NS, OUTPUT_PREFIX
    OUTPUT_NS     = Namespace(str(output_ns))
    OUTPUT_PREFIX = str(output_prefix) if output_prefix else ""
    if OUTPUT_PREFIX:
        JSONLD_CONTEXT[OUTPUT_PREFIX] = str(output_ns)

    return PapyrusConfig(
        namespaces=namespaces,
        target_class=URIRef(str(target_class)),
        graph=combined,
        output_path=project_root / str(output_rel),
    )


# ── Graph-based loaders ───────────────────────────────────────────────────────

def _subclasses(g: Graph, cls: URIRef) -> set[URIRef]:
    """BFS over rdfs:subClassOf to collect all (transitive) subclasses of cls."""
    result: set[URIRef] = set()
    queue = [cls]
    while queue:
        current = queue.pop()
        for sub in g.subjects(RDFS.subClassOf, current):
            if isinstance(sub, URIRef) and sub not in result:
                result.add(sub)
                queue.append(sub)
    return result


def load_document_classes(g: Graph, target_class: URIRef) -> dict[str, DocumentClass]:
    """
    Return {notation: DocumentClass} for every owl:Class that is a (transitive)
    subclass of target_class and carries a skos:notation.
    """
    candidates = _subclasses(g, target_class)
    classes: dict[str, DocumentClass] = {}

    for cls_uri in candidates:
        notations   = list(g.objects(cls_uri, SKOS.notation))
        definitions = list(g.objects(cls_uri, SKOS.definition))
        if notations and definitions:
            comment = g.value(cls_uri, RDFS.comment)
            classes[str(notations[0])] = DocumentClass(
                uri=cls_uri,
                notation=str(notations[0]),
                definition=str(definitions[0]),
                description=str(comment).strip() if comment else "",
            )

    if not classes:
        raise ValueError(
            f"No subclass of <{target_class}> with skos:notation found in the ontology graph"
        )
    return classes


def load_preferred_model(g: Graph) -> ModelConfig:
    """
    Return the ModelConfig for the model referenced by papyrus:this via papyrus:model.
    Raises ValueError if none is found.
    """
    PAPYRUS_NS = Namespace("http://example.org/tax-classifier/papyrus#")
    self_this = g.value(predicate=RDF.type, object=PAPYRUS_NS.Self)
    if self_this is None:
        raise ValueError("No papyrus:Self individual found in graph")
    model_uri = g.value(self_this, PAPYRUS_NS.model)
    if model_uri is None:
        raise ValueError("papyrus:this has no papyrus:model property")
    model_id = g.value(model_uri, LLM.modelId)
    label    = g.value(model_uri, RDFS.label)
    if not model_id:
        raise ValueError(f"Model {model_uri} has no llm:modelId")
    return ModelConfig(
        uri=model_uri,
        model_id=str(model_id),
        label=str(label) if label else str(model_uri),
    )


def prefixed_name(uri: URIRef) -> str:
    """Return a prefix:localname form for known namespaces, or the full URI string."""
    s = str(uri)
    for prefix, ns in JSONLD_CONTEXT.items():
        if s.startswith(ns):
            return f"{prefix}:{s[len(ns):]}"
    return s
