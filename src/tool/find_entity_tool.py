from ..models import DocumentHit

import logging
from .. import ontology as _ontology
from ..ontology import JSONLD_CONTEXT, prefixed_name
from ..shape_extractor import (
    _slug,
)
from rdflib import  URIRef
from rdflib.namespace import RDFS

logger = logging.getLogger(__name__)

FIND_ENTITY_TOOL: dict = {
    "name": "find_entity",
    "description": (
        "Search the knowledge graph for an existing entity. "
        "Returns matches (each with uri and known_properties), suggested_uri (stable URI "
        "to use when matches is empty), and available_properties (the exact property CURIEs "
        "defined in the schema for this class — use these as search keys, not guesses). "
        "Use for every object whose @id starts with '<RESOLVE', and for the root "
        "document stub before filling the template. "
        "If the document contains properties not in known_properties, include them in the "
        "extracted document — they will be merged as new facts. "
        "A differing value (e.g. a new address) is not an error — both values will be kept."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_uri": {
                "type": "string",
                "description": "Class URI or CURIE, e.g. foaf:Organization",
            },
            "properties": {
                "type": "object",
                "description": (
                    "Property-value pairs to match. Keys are CURIEs or slash-separated "
                    "property paths for nested values. "
                    "e.g. {\"fin:taxId\": \"7713759202\"} or {\"foaf:name\": \"ООО Дельта\"} "
                    "or {\"fin:issuer/foaf:name\": \"Zahnarztpraxis Liebermann\"}. "
                    "Fewer, more specific properties yield better results."
                ),
            },
        },
        "required": ["class_uri", "properties"],
    },
}

import re as _re
_DATE_RE  = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GYEAR_RE = _re.compile(r"^\d{4}$")

def _expand(curie: str) -> str:
    """Expand a CURIE to a full URI using JSONLD_CONTEXT."""
    if curie.startswith(("http", "urn")):
        return curie
    for prefix, ns in JSONLD_CONTEXT.items():
        if curie.startswith(f"{prefix}:"):
            return ns + curie[len(prefix) + 1:]
    return curie

def find_entity(self, class_uri: str, properties: dict) -> dict:
    expanded_class = _expand(class_uri)

    # If this is a document class (notation in our map), record the classification hit.
    notation = self._uri_to_notation.get(expanded_class) or self._uri_to_notation.get(class_uri)
    if notation and self._hit is None:
        self._hit = DocumentHit(
            category=notation,
            class_uri=expanded_class,
            confidence=0.0,   # will be updated by get_extraction_plan
            reason="",
        )
        if self._on_classified:
            self._on_classified(self._hit)

    # Build a stable suggested URI in the configured output namespace.
    class_local = _slug(class_uri.rsplit(":", 1)[-1].rsplit("/", 1)[-1].lower())
    name_value = (
        properties.get("foaf:name")
        or properties.get("fin:taxId")
        or properties.get("fin:registrationNumber")
        or next((v for v in properties.values() if isinstance(v, str) and v), None)
        or class_local
    )
    local = f"{class_local}_{_slug(str(name_value).lower())}"
    prefix = _ontology.OUTPUT_PREFIX
    suggested_uri = f"{prefix}:{local}" if prefix else str(_ontology.OUTPUT_NS[local])

    # Expand abstract superclasses to their known concrete subclasses so a
    # query for foaf:Agent also matches foaf:Person and foaf:Organization.
    concrete = {expanded_class}
    for sub in self.graph.subjects(RDFS.subClassOf, URIRef(expanded_class)):
        concrete.add(str(sub))
    if len(concrete) == 1:
        type_clause = f"  ?s a {_sparql_term(URIRef(expanded_class))} ."
    else:
        values = " ".join(_sparql_term(URIRef(c)) for c in concrete)
        type_clause = f"  ?s a ?_type . VALUES ?_type {{ {values} }}"
    clauses = [type_clause]
    var_counter = 0
    for prop, value in properties.items():
        if value is None or value == "null" or value == "":
            continue
        inline, filter_tmpl = _sparql_literal(value)
        if inline is None and filter_tmpl is None:
            continue
        segments = [seg.strip() for seg in prop.split("/")]

        if filter_tmpl is not None:
            # Typed value: bind to intermediate variable, then FILTER.
            # Multi-hop paths also need the intermediate chain below.
            subject = "?s"
            for seg in segments[:-1]:
                nxt = f"?_v{var_counter}"
                var_counter += 1
                clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(seg)))} {nxt} .")
                subject = nxt
            leaf_var = f"?_v{var_counter}"
            var_counter += 1
            clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(segments[-1])))} {leaf_var} .")
            clauses.append(f"  FILTER({filter_tmpl.format(i=leaf_var[1:])})")
        elif len(segments) == 1:
            clauses.append(f"  ?s {_sparql_term(URIRef(_expand(segments[0])))} {inline} .")
        else:
            # Multi-hop path: explicit intermediate variables.
            subject = "?s"
            for seg in segments[:-1]:
                nxt = f"?_v{var_counter}"
                var_counter += 1
                clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(seg)))} {nxt} .")
                subject = nxt
            clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(segments[-1])))} {inline} .")

    body = "SELECT ?s WHERE {\n" + "\n".join(clauses) + "\n}"
    query = _sparql_prefixes(body) + body

    # Query both the accumulated results and the ontology graph (which may
    # contain pre-declared known entities like persons or organisations).
    found: set[str] = set()
    for g in (self.results_graph, self.graph):
        if len(g) == 0:
            continue
        try:
            found.update(str(r.s) for r in g.query(query))
        except Exception as exc:
            logger.warning("agent | find_entity query failed on graph: %s", exc)

    # For each matched URI, collect its known properties from both graphs
    # so the LLM can compare them with what the current document says.
    matches = []
    for uri in found:
        known: dict[str, list] = {}
        for g in (self.graph, self.results_graph):
            for pred, obj in g.predicate_objects(URIRef(uri)):
                key = prefixed_name(pred)
                val = str(obj)
                known.setdefault(key, [])
                if val not in known[key]:
                    known[key].append(val)
        # Flatten single-value lists for readability
        flat = {k: (v[0] if len(v) == 1 else v) for k, v in known.items()}
        matches.append({"uri": uri, "known_properties": flat})

    logger.debug(
        "agent | find_entity %s props=%s → %d match(es)\nquery:\n%s",
        class_uri, properties, len(matches), query,
    )
    return {"matches": matches, "suggested_uri": suggested_uri}

def _sparql_prefixes(query_body: str) -> str:
    """Return SPARQL PREFIX declarations for namespaces used in query_body."""
    lines = [
        f"PREFIX {prefix}: <{ns}>"
        for prefix, ns in JSONLD_CONTEXT.items()
        if f"{prefix}:" in query_body
    ]
    return ("\n".join(lines) + "\n") if lines else ""

def _sparql_literal(value) -> tuple[str | None, str | None]:
    """
    Return (inline_literal, filter_expr) for a SPARQL clause, or (None, None) to skip.

    inline_literal  — used directly in the triple pattern:  ?s prop "value" .
    filter_expr     — requires an intermediate variable:
                        ?s prop ?_vN . FILTER(filter_expr(?_vN))

    Typed literals (dates, years) use FILTER(str(?var) = "...") because
    rdflib's JSON-LD parser may store xsd:date as a CURIE instead of a full
    URI, so direct typed-literal matching is unreliable.
    """
    if isinstance(value, bool):
        return ("true" if value else "false", None)
    if isinstance(value, (int, float)):
        return (str(value), None)
    if isinstance(value, str):
        safe = value.replace("\\", "\\\\").replace('"', '\\"')
        if _DATE_RE.match(value) or _GYEAR_RE.match(value):
            return (None, f'str(?_v{{i}}) = "{safe}"')
        return (f'"{safe}"', None)
    return (None, None)

def _sparql_term(uri: URIRef) -> str:
    """
    Return a CURIE if the prefix is declared in JSONLD_CONTEXT, else <full-uri>.
    Using a CURIE requires the matching PREFIX declaration in the query header;
    falling back to <uri> syntax works without any PREFIX declarations.
    """
    name = prefixed_name(uri)
    if ":" not in name or name.startswith("http") or name.startswith("urn"):
        return f"<{uri}>"
    return name
