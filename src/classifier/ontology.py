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
SELF = Namespace("http://example.org/tax-classifier/self#")

# Canonical @context used in JSON-LD extraction prompts and result parsing.
# Populated from self.ttl at startup via load_self(); the values below are
# only a fallback in case load_self() is not called.
JSONLD_CONTEXT: dict[str, str] = {
    "fin":  "http://example.org/financial/",
    "tax":  "http://example.org/tax-classifier/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "xsd":  "http://www.w3.org/2001/XMLSchema#",
}


@dataclass
class SelfConfig:
    """Configuration derived from data/self.ttl."""
    namespaces:   dict[str, str]  # prefix → namespace URI
    target_class: URIRef          # OWL class whose subclasses are classification targets
    graph:        Graph           # all local (+ optionally remote) ontologies merged


def load_self(self_path: Path, *, load_remote: bool = False) -> SelfConfig:
    """
    Parse data/self.ttl and return the project configuration.

    Loads every self:LocalOntology listed via self:hasOntology into a single
    combined graph.  If load_remote=True, also fetches self:RemoteOntology
    URLs (skipped with a warning on network error).

    Also updates the module-level JSONLD_CONTEXT dict in-place.
    """
    self_graph = Graph()
    self_graph.parse(self_path)
    project_root = self_path.parent.parent  # data/self.ttl → data/ → project/

    # ── Find the Self individual ──────────────────────────────────────────────
    self_individual = self_graph.value(predicate=RDF.type, object=SELF.Self)
    if self_individual is None:
        raise ValueError(f"{self_path}: no individual of type self:Self found")

    # ── Namespace map — read directly from @prefix declarations ───────────────
    # g.namespaces() returns every prefix bound in the file (plus rdflib defaults).
    # We collect only the prefixes that are explicitly claimed by an ontology
    # instance via self:prefix, so the result is intentional rather than implicit.
    declared_ns = dict(self_graph.namespaces())  # prefix → Namespace URI
    namespaces: dict[str, str] = {}
    for ont in self_graph.objects(self_individual, SELF.hasOntology):
        p = self_graph.value(ont, SELF.prefix)
        if p and str(p) in declared_ns:
            namespaces[str(p)] = str(declared_ns[str(p)])

    JSONLD_CONTEXT.clear()
    JSONLD_CONTEXT.update(namespaces)

    # ── Target class ──────────────────────────────────────────────────────────
    target_class = self_graph.value(self_individual, SELF.targetClass)
    if target_class is None:
        raise ValueError(f"{self_path}: self:this has no self:targetClass")

    # ── Build combined ontology graph ─────────────────────────────────────────
    combined = Graph()
    combined += self_graph  # self.ttl itself is part of the graph

    for ont in self_graph.objects(self_individual, SELF.hasOntology):
        ont_types = set(self_graph.objects(ont, RDF.type))

        if SELF.LocalOntology in ont_types:
            rel = self_graph.value(ont, SELF.relativePath)
            if rel is None:
                logger.warning("Local ontology <%s> has no self:relativePath — skipped", ont)
                continue
            path = project_root / str(rel)
            logger.debug("Loading local ontology from %s", path)
            combined.parse(path)

        elif SELF.RemoteOntology in ont_types:
            url = self_graph.value(ont, SELF.url)  # URIRef — set directly, no cast needed
            if not load_remote:
                logger.debug("Skipping remote ontology <%s> (pass load_remote=True to fetch)", url)
                continue
            if url is None:
                logger.warning("Remote ontology <%s> has no self:url — skipped", ont)
                continue
            try:
                logger.debug("Fetching remote ontology from %s", url)
                combined.parse(str(url))
            except Exception as exc:
                logger.warning("Could not fetch remote ontology <%s>: %s", url, exc)

    return SelfConfig(
        namespaces=namespaces,
        target_class=URIRef(str(target_class)),
        graph=combined,
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
    Return the ModelConfig for the model flagged with llm:preferred true.
    Raises ValueError if none is found.
    """
    for model_uri in g.subjects(RDF.type, LLM.Model):
        preferred = g.value(model_uri, LLM.preferred)
        if preferred and preferred.toPython() is True:
            model_id = g.value(model_uri, LLM.modelId)
            label    = g.value(model_uri, RDFS.label)
            if not model_id:
                raise ValueError(f"Preferred model {model_uri} has no llm:modelId")
            return ModelConfig(
                uri=model_uri,
                model_id=str(model_id),
                label=str(label) if label else str(model_uri),
            )

    raise ValueError("No model with llm:preferred true found in the ontology graph")


def prefixed_name(uri: URIRef) -> str:
    """Return a prefix:localname form for known namespaces, or the full URI string."""
    s = str(uri)
    for prefix, ns in JSONLD_CONTEXT.items():
        if s.startswith(ns):
            return f"{prefix}:{s[len(ns):]}"
    return s
