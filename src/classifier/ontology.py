"""Load OWL/RDF ontology data: document classes, properties, and model config."""

import json
import re
from pathlib import Path

from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import OWL, RDFS, SKOS, XSD

from .models import DocumentClass, ModelConfig, PropertyDef

TAX  = Namespace("http://example.org/tax-classifier/")
FIN  = Namespace("http://example.org/financial/")
LLM  = Namespace("http://example.org/llm#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")

# Canonical @context used in every JSON-LD extraction prompt and result parse.
JSONLD_CONTEXT: dict[str, str] = {
    "fin":  "http://example.org/financial/",
    "tax":  "http://example.org/tax-classifier/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "xsd":  "http://www.w3.org/2001/XMLSchema#",
}


def prefixed_name(uri: URIRef) -> str:
    """Return a prefix:localname form for known namespaces, or the full URI string."""
    s = str(uri)
    for prefix, ns in JSONLD_CONTEXT.items():
        if s.startswith(ns):
            return f"{prefix}:{s[len(ns):]}"
    return s


def _local_name(uri) -> str:
    s = str(uri)
    return s.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _to_snake(name: str) -> str:
    """Convert camelCase or PascalCase to snake_case."""
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    return s.lower()


def _first_sentence(text: str) -> str:
    """Return the first sentence of a (possibly multi-line) comment string."""
    flat = " ".join(text.split())
    end = flat.find(".")
    return flat[: end + 1].strip() if end != -1 else flat.strip()


def _parse_graph(rdf_path: Path) -> Graph:
    g = Graph()
    g.parse(rdf_path)
    return g


def _build_ancestor_map(g: Graph) -> dict[URIRef, set[URIRef]]:
    """
    Return {class_uri: {class_uri, parent, grandparent, …}} for every class
    in the graph by following rdfs:subClassOf transitively.
    """
    parents: dict[URIRef, set[URIRef]] = {}
    for child, _, parent in g.triples((None, RDFS.subClassOf, None)):
        if isinstance(child, URIRef) and isinstance(parent, URIRef):
            parents.setdefault(child, set()).add(parent)

    ancestors: dict[URIRef, set[URIRef]] = {}

    def _ancestors(uri: URIRef) -> set[URIRef]:
        if uri in ancestors:
            return ancestors[uri]
        result: set[URIRef] = {uri}
        for p in parents.get(uri, set()):
            result |= _ancestors(p)
        ancestors[uri] = result
        return result

    for cls in g.subjects(RDF.type, OWL.Class):
        _ancestors(cls)

    return ancestors


def load_document_classes(rdf_path: Path) -> dict[str, DocumentClass]:
    """
    Load OWL document classes from the categories ontology.
    Returns {notation: DocumentClass}.
    """
    g = _parse_graph(rdf_path)

    classes: dict[str, DocumentClass] = {}
    for cls_uri in g.subjects(RDF.type, OWL.Class):
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
        raise ValueError(f"No owl:Class entries with skos:notation found in {rdf_path}")
    return classes


def load_class_properties(rdf_path: Path) -> dict[str, list[PropertyDef]]:
    """
    Load OWL properties grouped by the document class they belong to.
    Returns {notation: [PropertyDef, ...]}.
    Domain matching follows rdfs:subClassOf transitively.
    """
    g = _parse_graph(rdf_path)

    notation_to_uri: dict[str, URIRef] = {}
    for cls_uri in g.subjects(RDF.type, OWL.Class):
        notations = list(g.objects(cls_uri, SKOS.notation))
        if notations:
            notation_to_uri[str(notations[0])] = cls_uri  # type: ignore[assignment]

    ancestors = _build_ancestor_map(g)

    domain_to_notations: dict[URIRef, list[str]] = {}
    for notation, cls_uri in notation_to_uri.items():
        for anc in ancestors.get(cls_uri, {cls_uri}):
            domain_to_notations.setdefault(anc, []).append(notation)

    result: dict[str, list[PropertyDef]] = {n: [] for n in notation_to_uri}

    for owl_type in (OWL.DatatypeProperty, OWL.ObjectProperty):
        for prop_uri in g.subjects(RDF.type, owl_type):
            label = g.value(prop_uri, RDFS.label)
            if not label:
                continue
            field_key = str(g.value(prop_uri, TAX.fieldKey) or _to_snake(_local_name(prop_uri)))
            rdf_range = g.value(prop_uri, RDFS.range) or XSD.string
            raw_comment = g.value(prop_uri, RDFS.comment)
            comment = _first_sentence(str(raw_comment)) if raw_comment else ""

            prop = PropertyDef(
                uri=prop_uri,
                field_key=field_key,
                label=str(label),
                rdf_range=rdf_range,
                comment=comment,
            )

            for domain_uri in g.objects(prop_uri, RDFS.domain):
                for notation in domain_to_notations.get(domain_uri, []):
                    result[notation].append(prop)

    return result


def load_preferred_model(rdf_path: Path) -> ModelConfig:
    """
    Load the model flagged with llm:preferred true from the models ontology.
    Raises ValueError if none is found.
    """
    g = Graph()
    g.parse(rdf_path)

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

    raise ValueError(f"No model with llm:preferred true found in {rdf_path}")


def build_category_descriptions(
    doc_classes: dict[str, DocumentClass],
    class_props: dict[str, list[PropertyDef]],
) -> dict[str, str]:
    """Return {notation: full_description} combining the class description with its field list."""
    result: dict[str, str] = {}
    for notation, cls in doc_classes.items():
        base = cls.description or cls.definition
        props = class_props.get(notation, [])
        if props:
            labels = ", ".join(p.label for p in props)
            result[notation] = f"{base}\nFields: {labels}."
        else:
            result[notation] = base
    return result


def build_jsonld_extraction_prompt(
    doc_class: DocumentClass,
    class_props: list[PropertyDef],
) -> str:
    """
    Build a prompt asking the LLM to return document details as JSON-LD.
    The LLM already has the document in context from the classification turn.
    """
    context_str = json.dumps(JSONLD_CONTEXT, indent=2)
    class_qname = prefixed_name(doc_class.uri)

    prop_lines = "\n".join(
        f"  {prefixed_name(p.uri)}"
        + (f" — {p.comment}" if p.comment else "")
        for p in class_props
    )

    return (
        f"Extract all available fields from this document and return as JSON-LD.\n\n"
        f"Use exactly this @context:\n{context_str}\n\n"
        f'Set "@type" to "{class_qname}" on the root object.\n\n'
        f"For nested agents (issuer, recipient, counterparty, etc.):\n"
        f'  - "@type": "foaf:Person" for individuals\n'
        f'  - "@type": "foaf:Organization" for companies, practices, institutions\n\n'
        f"For monetary amounts:\n"
        f'  {{"@type": "tax:MonetaryAmount", "tax:numericValue": 115.84, "tax:currency": "EUR"}}\n\n'
        f'For dates: {{"@value": "2024-01-15", "@type": "xsd:date"}}\n\n'
        f"Available properties:\n{prop_lines}\n\n"
        f"Return JSON-LD only. Use null for any field not found in the document."
    )
