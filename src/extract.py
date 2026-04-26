"""LLM-driven property extraction (step 7 of the pipeline).

Given a classified document and the live combined graph:

1. Walk ``rdfs:subClassOf*`` from the chosen class to collect every property
   whose ``rdfs:domain`` matches an ancestor.
2. For object-typed properties, also collect the datatype properties of the
   range class (one level of nesting).
3. Ask the LLM, in a single call, to fill in as many of those properties as
   the document supports. Open-world: if a value isn't there, the LLM omits
   it. No SHACL / cardinality enforcement.
4. Walk the JSON response and emit triples — minting URIs for nested entities,
   handling list values (one triple per item).
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from rdflib import Dataset, Graph, Literal, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from src.classifier import _parse_json_response
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)

_MARKDOWN_BUDGET = 32_000

# Properties whose URI starts with one of these are foundational vocabulary
# (docgraph internals, PROV-O, Part 14, DC Terms, OWL/RDF/XSD). The LLM should
# extract domain content, not provenance / structural / meta-ontology slots.
_FOUNDATION_PREFIXES = (
    "http://example.org/docgraph/meta#",
    "http://www.w3.org/ns/prov#",
    "http://standards.iso.org/iso/15926/part14/",
    "http://purl.org/dc/terms/",
    "http://www.w3.org/2002/07/owl#",
    "http://www.w3.org/2000/01/rdf-schema#",
    "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "http://www.w3.org/2001/XMLSchema#",
)


def _is_foundational(uri: URIRef) -> bool:
    return any(str(uri).startswith(p) for p in _FOUNDATION_PREFIXES)


_XSD_LABEL = {
    XSD.string:   "string",
    XSD.integer:  "integer",
    XSD.decimal:  "decimal",
    XSD.date:     "date (YYYY-MM-DD)",
    XSD.dateTime: "datetime (ISO 8601)",
    XSD.boolean:  "boolean (true/false)",
}


@dataclass
class PropertyDef:
    uri: URIRef
    local_name: str         # JSON key the LLM should use
    label: str
    comment: str
    range_uri: URIRef | None
    is_datatype: bool       # owl:DatatypeProperty vs owl:ObjectProperty


@dataclass
class ClassDef:
    uri: URIRef
    local_name: str
    label: str
    comment: str
    properties: list[PropertyDef] = field(default_factory=list)


# ─── Schema discovery ────────────────────────────────────────────────────────

def class_def(ds: Dataset, class_uri: URIRef) -> ClassDef:
    label   = ds.value(class_uri, RDFS.label)
    comment = ds.value(class_uri, RDFS.comment)
    return ClassDef(
        uri=class_uri,
        local_name=_local_name(class_uri),
        label=str(label) if label is not None else _local_name(class_uri),
        comment=str(comment) if comment is not None else "",
        properties=applicable_properties(ds, class_uri),
    )


def applicable_properties(ds: Dataset, class_uri: URIRef) -> list[PropertyDef]:
    """Properties whose ``rdfs:domain`` is *class_uri* or any ancestor."""
    ancestors = _ancestors(ds, class_uri)

    seen: dict[URIRef, PropertyDef] = {}
    for ancestor in ancestors:
        for prop in ds.subjects(RDFS.domain, ancestor):
            if not isinstance(prop, URIRef) or prop in seen:
                continue
            if _is_foundational(prop):
                continue
            types = set(ds.objects(prop, RDF.type))
            is_datatype = OWL.DatatypeProperty in types
            is_object   = OWL.ObjectProperty   in types
            if not (is_datatype or is_object):
                continue
            label   = ds.value(prop, RDFS.label)
            comment = ds.value(prop, RDFS.comment)
            range_  = ds.value(prop, RDFS.range)
            seen[prop] = PropertyDef(
                uri=prop,
                local_name=_local_name(prop),
                label=str(label) if label is not None else _local_name(prop),
                comment=str(comment) if comment is not None else "",
                range_uri=range_ if isinstance(range_, URIRef) else None,
                is_datatype=is_datatype,
            )
    return sorted(seen.values(), key=lambda p: p.local_name)


def nested_class_defs(ds: Dataset, props: list[PropertyDef]) -> dict[URIRef, ClassDef]:
    """For every object property's range class, build a ClassDef (datatype props only).

    Skips XSD ranges and ranges we've already collected. Only includes datatype
    properties of the nested class — we don't recurse further to keep the
    prompt and JSON shape manageable.
    """
    out: dict[URIRef, ClassDef] = {}
    for p in props:
        if p.is_datatype or p.range_uri is None:
            continue
        if _is_foundational(p.range_uri):
            continue          # owl:Class, prov:Entity, etc. — not user content
        if p.range_uri in out:
            continue
        cd = class_def(ds, p.range_uri)
        cd.properties = [pp for pp in cd.properties if pp.is_datatype]
        out[p.range_uri] = cd
    return out


# ─── Prompt construction ─────────────────────────────────────────────────────

def build_extraction_prompt(
    markdown: str,
    root: ClassDef,
    nested: dict[URIRef, ClassDef],
) -> str:
    parts = [
        f"You are extracting data from a document classified as **{root.label}**.",
    ]
    if root.comment:
        parts.append(_collapse(root.comment))
    parts.append("")
    parts.append("Document content:")
    parts.append("---")
    parts.append(_truncate(markdown, _MARKDOWN_BUDGET))
    parts.append("---")
    parts.append("")
    parts.append(
        "Extract values for as many of the following properties as the document supports. "
        "OMIT any property whose value is not present in or directly inferable from the document. "
        "Do not invent values."
    )
    parts.append("")

    parts.append(f"## Properties of {root.label}")
    for p in root.properties:
        parts.append(_format_property(p, nested))
    if nested:
        parts.append("")
        parts.append("## Schemas for nested object values")
        for cd in nested.values():
            parts.append(f"### {cd.label}  (<{cd.uri}>)")
            for p in cd.properties:
                parts.append(_format_property(p, nested))

    parts.append("")
    parts.append(
        "Reply with a single JSON object — no commentary, no code fences. Use the\n"
        "property local-name as the key. For object properties, provide a nested JSON\n"
        "object whose keys are the nested schema's local-names. For properties that\n"
        "occur multiple times in the document, return a JSON list. Use ISO 8601 dates.\n"
        "For decimals, give raw numbers (no currency symbols, dot as decimal separator)."
    )
    return "\n".join(parts)


def _format_property(p: PropertyDef, nested: dict[URIRef, ClassDef]) -> str:
    if p.is_datatype:
        kind = _XSD_LABEL.get(p.range_uri, "value")
    else:
        nest = nested.get(p.range_uri) if p.range_uri else None
        kind = f"object: {nest.label}" if nest else "object"
    line = f"  - `{p.local_name}`  ({kind}) — {p.label}"
    if p.comment:
        snippet = _collapse(p.comment)
        if len(snippet) > 220:
            snippet = snippet[:217] + "..."
        line += f"\n      {snippet}"
    return line


# ─── LLM call ────────────────────────────────────────────────────────────────

def extract_instance_data(
    markdown: str,
    root: ClassDef,
    nested: dict[URIRef, ClassDef],
    client,
    model: ModelConfig,
) -> dict:
    if not root.properties:
        return {}
    prompt = build_extraction_prompt(markdown, root, nested)
    meta = (f"{model.model_id}  max_tokens=2048  root={root.label}  "
            f"props={len(root.properties)}+{sum(len(c.properties) for c in nested.values())}")
    log_prompt("extract_instance_data", prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    log_response("extract_instance_data", raw, logger=logger, metadata=meta, as_json=True)
    try:
        return _parse_json_response(raw) or {}
    except Exception as exc:
        logger.warning("extract_instance_data | failed to parse JSON: %s", exc)
        return {}


# ─── Triple emission ─────────────────────────────────────────────────────────

def emit_triples(
    g: Graph,
    subject: URIRef,
    data: dict,
    root: ClassDef,
    nested: dict[URIRef, ClassDef],
    *,
    base_uri: str,
    path: str = "",
) -> int:
    """Walk *data*, emit triples on *subject*. Returns triple count added."""
    by_local = {p.local_name: p for p in root.properties}
    added = 0

    for key, value in data.items():
        prop = by_local.get(key)
        if prop is None:
            logger.debug("emit_triples | unknown key %r (skipping)", key)
            continue
        values = value if isinstance(value, list) else [value]

        for i, v in enumerate(values):
            if v is None or v == "":
                continue

            if prop.is_datatype:
                lit = _coerce_literal(v, prop.range_uri)
                if lit is not None:
                    g.add((subject, prop.uri, lit))
                    added += 1
            else:
                if not isinstance(v, dict):
                    logger.debug("emit_triples | object value for %r is not dict (got %s)",
                                 key, type(v).__name__)
                    continue

                # Mint a URI for the nested entity — readable, deterministic, queryable.
                segment = f"{path}/{key}" if path else key
                if len(values) > 1:
                    segment = f"{segment}-{i + 1}"
                child = URIRef(f"{base_uri}/{segment}")

                g.add((subject, prop.uri, child))
                if prop.range_uri:
                    g.add((child, RDF.type, prop.range_uri))
                added += 1

                nested_def = nested.get(prop.range_uri) if prop.range_uri else None
                if nested_def:
                    added += emit_triples(g, child, v, nested_def, nested,
                                          base_uri=base_uri, path=segment)
    return added


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _ancestors(ds: Dataset, class_uri: URIRef) -> set[URIRef]:
    seen: set[URIRef] = set()
    stack = [class_uri]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for parent in ds.objects(cur, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent not in seen:
                stack.append(parent)
    return seen


def _local_name(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s or str(uri)


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    head = budget * 3 // 4
    tail = budget - head - 20
    return f"{text[:head]}\n\n[... {len(text) - head - tail} chars truncated ...]\n\n{text[-tail:]}"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _coerce_literal(value: Any, range_uri: URIRef | None) -> Literal | None:
    if isinstance(value, bool):
        return Literal(value, datatype=XSD.boolean)
    if isinstance(value, int) and range_uri != XSD.decimal:
        return Literal(value, datatype=XSD.integer)
    if isinstance(value, float):
        return Literal(value, datatype=XSD.decimal)

    s = str(value).strip()
    if not s:
        return None

    if range_uri == XSD.integer:
        try: return Literal(int(s), datatype=XSD.integer)
        except ValueError: return Literal(s)
    if range_uri == XSD.decimal:
        # Tolerate "1.234,56" (German) and "1,234.56" (US) — strip thousands sep.
        cleaned = s.replace(" ", "")
        if cleaned.count(",") and cleaned.count("."):
            # Whichever is rightmost is the decimal mark.
            if cleaned.rfind(",") > cleaned.rfind("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")
        try: return Literal(cleaned, datatype=XSD.decimal)
        except Exception: return Literal(s)
    if range_uri == XSD.date:
        return Literal(s, datatype=XSD.date)
    if range_uri == XSD.dateTime:
        return Literal(s, datatype=XSD.dateTime)
    if range_uri == XSD.boolean:
        return Literal(s.strip().lower() in ("true", "yes", "1"), datatype=XSD.boolean)
    return Literal(s, datatype=range_uri) if range_uri else Literal(s)
