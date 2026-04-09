"""Load OWL/RDF ontology data: document classes, properties, and model config."""

import re
from pathlib import Path

from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import OWL, RDFS, SKOS, XSD

from .models import DocumentClass, ModelConfig, PropertyDef

TAX  = Namespace("http://example.org/tax-classifier/")
FIN  = Namespace("http://example.org/financial/")
LLM  = Namespace("http://example.org/llm#")
FOAF = Namespace("http://xmlns.com/foaf/0.1/")

# Object-property ranges extracted as a flat string (just a name/description).
_EXTRACTABLE_OBJ_RANGES: frozenset[URIRef] = frozenset({
    FIN.Service,
    FIN.Product,
})

# Object-property ranges extracted as a single nested JSON object.
# Each entry must have DatatypeProperties with rdfs:domain set to that class.
_COMPOUND_OBJ_RANGES: frozenset[URIRef] = frozenset({
    FOAF.Agent,
    FOAF.Person,
    FOAF.Organization,
})

# Object-property ranges extracted as a JSON array of nested objects.
# Each entry must have DatatypeProperties with rdfs:domain set to that class.
_COMPOUND_LIST_RANGES: frozenset[URIRef] = frozenset({
    FIN.LineItem,
})


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


def _build_leaf_prop(g: Graph, prop_uri: URIRef, is_obj: bool) -> PropertyDef | None:
    """
    Build a PropertyDef for a leaf (non-compound) property.
    Returns None for structural object links that cannot be extracted as scalars.
    """
    explicit_key = g.value(prop_uri, TAX.fieldKey)
    field_key = str(explicit_key) if explicit_key else _to_snake(_local_name(prop_uri))
    label     = g.value(prop_uri, RDFS.label) or field_key
    rdf_range = g.value(prop_uri, RDFS.range) or (FOAF.Agent if is_obj else XSD.string)
    is_monetary = is_obj and rdf_range == TAX.MonetaryAmount

    if is_obj and not is_monetary and rdf_range not in _EXTRACTABLE_OBJ_RANGES:
        return None

    raw_comment = g.value(prop_uri, RDFS.comment)
    comment = _first_sentence(str(raw_comment)) if raw_comment else ""

    return PropertyDef(
        uri=prop_uri,
        field_key=field_key,
        label=str(label),
        rdf_range=rdf_range,
        is_object_property=is_obj and not is_monetary,
        is_monetary=is_monetary,
        comment=comment,
    )


def _load_compound_item_schema(g: Graph, cls_uri: URIRef) -> list[PropertyDef]:
    """
    Load the extractable leaf sub-properties of a compound range class
    (e.g. fin:LineItem). Only DatatypeProperties and extractable ObjectProperties
    whose rdfs:domain includes cls_uri are returned.
    """
    props: list[PropertyDef] = []
    for owl_type, is_obj in [(OWL.DatatypeProperty, False), (OWL.ObjectProperty, True)]:
        for prop_uri in g.subjects(RDF.type, owl_type):
            if cls_uri not in set(g.objects(prop_uri, RDFS.domain)):
                continue
            p = _build_leaf_prop(g, prop_uri, is_obj)
            if p is not None:
                props.append(p)
    return props


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

    Object properties are handled in four ways:
    - _EXTRACTABLE_OBJ_RANGES → extracted as flat strings (is_object_property=True)
    - _COMPOUND_OBJ_RANGES    → extracted as a single nested object (is_compound_object=True)
    - _COMPOUND_LIST_RANGES   → extracted as an array of nested objects (is_compound_list=True)
    - everything else         → skipped (structural links)
    """
    g = _parse_graph(rdf_path)

    # Pre-load item schemas for all compound range classes (both object and list)
    compound_schemas: dict[URIRef, list[PropertyDef]] = {
        cls_uri: _load_compound_item_schema(g, cls_uri)
        for cls_uri in _COMPOUND_LIST_RANGES | _COMPOUND_OBJ_RANGES
    }

    # Build notation → class URI map
    notation_to_uri: dict[str, URIRef] = {}
    for cls_uri in g.subjects(RDF.type, OWL.Class):
        notations = list(g.objects(cls_uri, SKOS.notation))
        if notations:
            notation_to_uri[str(notations[0])] = cls_uri  # type: ignore[assignment]

    # Ancestor sets for subclass-aware domain matching
    ancestors = _build_ancestor_map(g)

    # domain_uri → [notation, …]
    domain_to_notations: dict[URIRef, list[str]] = {}
    for notation, cls_uri in notation_to_uri.items():
        for anc in ancestors.get(cls_uri, {cls_uri}):
            domain_to_notations.setdefault(anc, []).append(notation)

    result: dict[str, list[PropertyDef]] = {n: [] for n in notation_to_uri}

    for owl_type, is_obj in [(OWL.DatatypeProperty, False), (OWL.ObjectProperty, True)]:
        for prop_uri in g.subjects(RDF.type, owl_type):
            explicit_key = g.value(prop_uri, TAX.fieldKey)
            field_key = str(explicit_key) if explicit_key else _to_snake(_local_name(prop_uri))
            label     = g.value(prop_uri, RDFS.label) or field_key
            rdf_range = g.value(prop_uri, RDFS.range) or (FOAF.Agent if is_obj else XSD.string)
            is_monetary     = is_obj and rdf_range == TAX.MonetaryAmount
            is_compound     = is_obj and rdf_range in _COMPOUND_LIST_RANGES
            is_compound_obj = is_obj and rdf_range in _COMPOUND_OBJ_RANGES

            # Skip non-extractable structural object links
            if is_obj and not is_monetary and not is_compound and not is_compound_obj \
                    and rdf_range not in _EXTRACTABLE_OBJ_RANGES:
                continue

            raw_comment = g.value(prop_uri, RDFS.comment)
            comment = _first_sentence(str(raw_comment)) if raw_comment else ""

            has_schema = is_compound or is_compound_obj
            prop = PropertyDef(
                uri=prop_uri,
                field_key=field_key,
                label=str(label),
                rdf_range=rdf_range,
                is_object_property=is_obj and not is_monetary and not is_compound and not is_compound_obj,
                is_monetary=is_monetary,
                comment=comment,
                is_compound_list=is_compound,
                is_compound_object=is_compound_obj,
                item_schema=compound_schemas.get(rdf_range, []) if has_schema else [],
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


def load_categories(rdf_path: Path) -> dict[str, str]:
    """Returns {notation: description} for building the classification prompt.
    Falls back to skos:definition when rdfs:comment is absent."""
    return {
        cls.notation: cls.description or cls.definition
        for cls in load_document_classes(rdf_path).values()
    }


def build_category_descriptions(
    doc_classes: dict[str, DocumentClass],
    class_props: dict[str, list[PropertyDef]],
) -> dict[str, str]:
    """
    Return {notation: full_description} where full_description combines the
    class description with a structured list of its fields and sub-fields
    derived from the ontology properties.

    Format per class:
        <description>
        Fields: field_label, field_label, ...
        <compound_label>: [sub_label, sub_label, ...]
    """
    result: dict[str, str] = {}
    for notation, cls in doc_classes.items():
        base = cls.description or cls.definition
        props = class_props.get(notation, [])

        scalar_labels: list[str] = []
        compound_parts: list[str] = []

        for p in props:
            if p.is_compound_list:
                sub_labels = ", ".join(s.label for s in p.item_schema)
                compound_parts.append(f"{p.label}: [{sub_labels}]")
            elif p.is_compound_object:
                sub_labels = ", ".join(s.label for s in p.item_schema)
                compound_parts.append(f"{p.label}: {{{sub_labels}}}")
            else:
                scalar_labels.append(p.label)

        lines = [base]
        if scalar_labels:
            lines.append("Fields: " + ", ".join(scalar_labels) + ".")
        for cp in compound_parts:
            lines.append(cp)

        result[notation] = "\n".join(lines)

    return result


_RANGE_HINTS: dict[str, str] = {
    str(XSD.date):           " (ISO date YYYY-MM-DD)",
    str(XSD.gYear):          " (4-digit year)",
    str(XSD.boolean):        " (true or false)",
    str(XSD.decimal):        " (decimal number)",
    str(TAX.MonetaryAmount): " (amount and ISO 4217 currency, e.g. 115.84 EUR)",
    str(FIN.Service):        " (service name or description)",
    str(FIN.Product):        " (product name or SKU)",
}


def _prop_line(p: PropertyDef, indent: str = "  ") -> str:
    hint    = _RANGE_HINTS.get(str(p.rdf_range), "")
    context = f" — {p.comment}" if p.comment else ""
    return f'{indent}"{p.field_key}": "<{p.label}{context}{hint}>"'


def build_extraction_prompt(properties: list[PropertyDef]) -> str:
    """
    Build an LLM prompt that asks for the specific properties of a document class.
    Scalar properties render as plain JSON fields.
    Compound object properties (e.g. issuer) render as a nested JSON object.
    Compound list properties (e.g. line_item) render as a JSON array of objects.
    Sub-property schemas are derived from the range class's own properties.
    """
    lines: list[str] = []
    for p in properties:
        if p.is_compound_list:
            item_lines = [_prop_line(sub, indent="      ") for sub in p.item_schema]
            inner = ",\n".join(item_lines)
            lines.append(
                f'  "{p.field_key}": [\n'
                f'    {{\n'
                f'{inner}\n'
                f'    }}\n'
                f'  ]'
            )
        elif p.is_compound_object:
            item_lines = [_prop_line(sub, indent="    ") for sub in p.item_schema]
            # When the range is the abstract foaf:Agent, inject a type discriminator
            # so Claude picks the concrete subclass (foaf:Person or foaf:Organization).
            if p.rdf_range == FOAF.Agent:
                item_lines.insert(0, '    "type": "<person or organization>"')
            inner = ",\n".join(item_lines)
            lines.append(
                f'  "{p.field_key}": {{\n'
                f'{inner}\n'
                f'  }}'
            )
        else:
            lines.append(_prop_line(p))

    return (
        "Extract the following fields from this document. "
        "Use null for any field not found. "
        "For array fields extract one object per entry found in the document.\n\n"
        "Respond with JSON only:\n{\n"
        + ",\n".join(lines)
        + "\n}"
    )
