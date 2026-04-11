"""Shape utilities: shape-to-template and related helpers.

The extraction shape defines the bounded extraction context:

  sh:node   → recurse (primary object, owned by the document)
  sh:class  → identity stub only (secondary object, resolved separately)
  sh:node + sh:class → recurse with explicit @type hint
  absent    → outside this bounded context, not extracted
"""

import re
from pathlib import Path

from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import RDFS, XSD

from .ontology import JSONLD_CONTEXT, prefixed_name

SH  = Namespace("http://www.w3.org/ns/shacl#")
TAX = Namespace("http://example.org/tax-classifier/")


# ── URI helpers ───────────────────────────────────────────────────────────────

def _curie(uri: str) -> str:
    return prefixed_name(URIRef(uri))


def _expand(s: str) -> str:
    for prefix, ns in JSONLD_CONTEXT.items():
        if s.startswith(f"{prefix}:"):
            return ns + s[len(prefix) + 1:]
    return s


def _local(uri: str) -> str:
    return uri.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9\-_.]", "-", s).strip("-")


def _first_sentence(text: str) -> str:
    """Return the first sentence, avoiding splits on abbreviations like e.g. / i.e."""
    # Collapse whitespace, then split only on ". " followed by a capital or end-of-string
    flat = " ".join(text.split())
    match = re.search(r"(?<!\be\.g)(?<!\bi\.e)\.\s+[A-Z]", flat)
    return flat[: match.start() + 1].strip() if match else flat.strip()


def _mint_uri(parent_uri: str, prop_uri: str, index: int | None = None) -> str:
    uri = f"{parent_uri}_{_slug(_local(prop_uri))}"
    return f"{uri}_{index}" if index is not None else uri


def _concrete_type_hint(ont_graph: Graph, class_uri: URIRef) -> str:
    """
    Return a @type placeholder for a RESOLVE object.

    Concrete class  → bare CURIE (already the resolved value, no angle brackets).
    Abstract class  → "<BaseClass — one of: sub1 | sub2 | ...>" (instruction).
    """
    base = _curie(str(class_uri))
    subs = sorted(
        _curie(str(s))
        for s in ont_graph.subjects(RDFS.subClassOf, class_uri)
    )
    if subs:
        return f"<RESOLVE:{base} — one of: {' | '.join(subs)}>"
    return base


# ── Shapes loading ────────────────────────────────────────────────────────────

def load_shapes(shapes_path: Path) -> Graph:
    g = Graph()
    g.parse(shapes_path)
    return g


def find_extraction_shape(shapes_graph: Graph, class_uri: str) -> URIRef | None:
    """Return the extraction shape linked via tax:extractionShape, or None."""
    return shapes_graph.value(URIRef(class_uri), TAX.extractionShape)  # type: ignore[return-value]


# ── RDF list traversal ────────────────────────────────────────────────────────

def _rdf_list(g: Graph, head) -> list:
    items = []
    node = head
    while node and node != RDF.nil:
        first = g.value(node, RDF.first)
        if first is not None:
            items.append(first)
        node = g.value(node, RDF.rest)
    return items


# ── Template builder ──────────────────────────────────────────────────────────

def shape_to_template(
    shapes_graph: Graph,
    ont_graph: Graph,
    shape_uri: URIRef,
    node_uri: str,
    type_override: str | None = None,
    _visited: frozenset = frozenset(),
    _seen_hints: set | None = None,
) -> dict:
    """
    Recursively render a SHACL extraction shape as a JSON-LD template.

    sh:datatype              → "<xsd:type — first sentence of rdfs:comment>"
    sh:node (+ sh:class)     → recurse with that shape; use sh:class for @type
    sh:class (no sh:node)    → identity stub: @type + foaf:name only
    no sh:maxCount / > 1     → value is wrapped in a list (array property)

    Each property hint (rdfs:comment) is emitted only once across the whole template:
    the first occurrence carries the full hint; subsequent ones use just the bare type.
    """
    if shape_uri in _visited:
        stub: dict = {"@id": _curie(node_uri)}
        if type_override:
            stub["@type"] = type_override
        return stub
    _visited = _visited | {shape_uri}

    # Shared mutable set — allocated once at the top call, threaded through recursion.
    if _seen_hints is None:
        _seen_hints = set()

    target_class = shapes_graph.value(shape_uri, SH.targetClass)
    rdf_type = type_override or (str(target_class) if target_class else None)
    is_inline = bool(shapes_graph.value(shape_uri, TAX.inline))

    template: dict = {} if is_inline else {"@id": _curie(node_uri)}
    if rdf_type:
        # rdf_type may be a full URI, an already-CURIE string, or a placeholder
        if rdf_type.startswith("http"):
            template["@type"] = _curie(rdf_type)
        else:
            template["@type"] = rdf_type

    for prop_bn in shapes_graph.objects(shape_uri, SH.property):
        path = shapes_graph.value(prop_bn, SH.path)
        if not path:
            continue

        prop_key    = _curie(str(path))
        datatype    = shapes_graph.value(prop_bn, SH.datatype)
        node_shape  = shapes_graph.value(prop_bn, SH.node)
        sh_class    = shapes_graph.value(prop_bn, SH["class"])
        or_head     = shapes_graph.value(prop_bn, SH["or"])
        max_count   = shapes_graph.value(prop_bn, SH.maxCount)

        is_array = max_count is None or int(str(max_count)) > 1

        # rdfs:comment → hint, but only on the first occurrence of this property key.
        raw_comment = ont_graph.value(path, RDFS.comment)
        if raw_comment and prop_key not in _seen_hints:
            hint = _first_sentence(str(raw_comment))
            _seen_hints.add(prop_key)
        else:
            hint = ""

        if datatype:
            type_local  = _local(str(datatype))
            placeholder = f"<{type_local} — {hint}>" if hint else f"<{type_local}>"
            value       = [placeholder] if is_array else placeholder

        elif node_shape:
            # Recurse; sh:class provides the @type hint for the child node.
            # Expand abstract classes to their known concrete subclasses from the ontology.
            child_uri = _mint_uri(node_uri, str(path))
            type_hint = _concrete_type_hint(ont_graph, sh_class) if sh_class else None
            child = shape_to_template(
                shapes_graph, ont_graph, node_shape, child_uri,
                type_override=type_hint,
                _visited=_visited,
                _seen_hints=_seen_hints,
            )
            # If sh:class is present and the node shape is not inline, the object
            # is a secondary entity (exists independently of this document).
            # Replace the generated @id with RESOLVE: so the agent knows to call find_entity.
            node_is_inline = bool(shapes_graph.value(node_shape, TAX.inline))
            if sh_class and not node_is_inline:
                child["@id"] = "<RESOLVE>"
            value = [child] if is_array else child

        elif or_head:
            # Polymorphic property: collect all branch types + merge their fields
            branches = _rdf_list(shapes_graph, or_head)
            type_labels, merged_props = [], {}
            for branch_bn in branches:
                branch_shape = shapes_graph.value(branch_bn, SH.node)
                branch_class = shapes_graph.value(branch_bn, SH["class"])
                label = _curie(str(branch_class or branch_shape or ""))
                if label:
                    type_labels.append(label)
                if branch_shape:
                    sub = shape_to_template(
                        shapes_graph, ont_graph, branch_shape,
                        _mint_uri(node_uri, str(path)), _visited=_visited,
                        _seen_hints=_seen_hints,
                    )
                    for k, v in sub.items():
                        merged_props.setdefault(k, v)
            child = {
                "@id":   _curie(_mint_uri(node_uri, str(path))),
                "@type": f"<one of: {' | '.join(type_labels)}>",
                **{k: v for k, v in merged_props.items() if k not in ("@id", "@type")},
            }
            value = [child] if is_array else child

        elif sh_class:
            # Secondary object — exists independently of this document.
            # @id is marked RESOLVE: so the agent knows it must call find_entity.
            curie_class = _curie(str(sh_class))
            child = {
                "@id":       "<RESOLVE>",
                "@type":     _concrete_type_hint(ont_graph, sh_class),
                "foaf:name": "<string>",
            }
            value = [child] if is_array else child

        else:
            continue

        template[prop_key] = value

    return template


