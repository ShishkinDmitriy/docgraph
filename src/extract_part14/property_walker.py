"""M2 stage 2 — per-entity property extraction for the part14 pipeline.

For each entity extracted in stage 1, walk the properties whose `rdfs:domain`
includes that entity's class (or its supers), and run one small LLM call per
property scoped to:
  - that entity's supporting quotes (the document context that justified its
    extraction in the first place)
  - a small document-context block (title, dates, headers — stable info
    that's often relevant to property values but not in the entity's quotes)

Properties are filtered before the LLM is called:
  - Inverse pairs: only the "has..." direction is asked; the inverse triple
    derives automatically (or is left for downstream reasoning).
  - Sub-properties: only the most specialized property is asked; if the LLM
    proposes `lis:hasArrangedPart`, the parent `lis:hasPart` is implied.
  - `dg:extractable false` annotations: skipped entirely.

Property values:
  - If the property's range is a datatype (xsd:*), the LLM returns a string
    that's parsed into a Literal of that type.
  - If the range is a class, the LLM is given the list of known entities (with
    their types) and returns either an entity name (resolved to an existing
    URI) or an indication that no known entity matches (skipped — could mint
    a stub in a later batch).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from src.extract_part14 import axioms
from src.extract_part14.rdl import RdlResolver
from src.extract_part14.walker import DG, LIS, OA, ExtractedEntity
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)


# ── Property selection ────────────────────────────────────────────────────

def extractable_properties_for(entity_type: URIRef, ontology: Graph) -> list[URIRef]:
    """All extractable properties applicable to *entity_type*.

    Combines two sources:
      - DOMAIN-MATCHED: properties whose `rdfs:domain` is *entity_type* or a
        super-class of it.
      - DOMAIN-LESS: properties without any `rdfs:domain` declaration.
        POSC's LIS-14 leaves ~50 of 66 properties domain-less by design
        (lis:approvedOn, lis:hasRole, lis:hasBeginning, lis:createdBy, etc.) —
        meant to apply universally where they semantically fit. The LLM picks
        per-context.

    Inverses, parent (sub)properties, and dg:extractable=false are filtered out.
    """
    LIS_NS = str(LIS)
    raw = (
        axioms.properties_of(ontology, entity_type, include_inherited=True)
        + axioms.domain_less_properties(ontology, namespace=LIS_NS)
    )
    # Dedup while preserving order (early entries are domain-matched, may be more relevant)
    seen_props: set[URIRef] = set()
    unique_raw: list[URIRef] = []
    for p in raw:
        if p in seen_props:
            continue
        seen_props.add(p)
        unique_raw.append(p)
    raw = unique_raw

    # Filter dg:extractable false
    extractable = [p for p in raw if axioms.is_extractable(ontology, p)]

    # Drop properties that have an inverse pair where the OTHER direction is
    # also extractable — keep one direction. Heuristic: prefer the "has..."
    # form (forward direction), fall back to alphabetical for ties.
    seen_inverses: set[URIRef] = set()
    deduplicated_inverse: list[URIRef] = []
    for p in extractable:
        inv = axioms.inverse_of(ontology, p)
        if inv is not None and inv in extractable:
            if p in seen_inverses:
                continue       # already kept its partner
            # Prefer the "has..." direction; otherwise lexicographic
            p_local   = _local(p)
            inv_local = _local(inv)
            if p_local.startswith("has") and not inv_local.startswith("has"):
                keep = p
            elif inv_local.startswith("has") and not p_local.startswith("has"):
                keep = inv
            else:
                keep = p if str(p) < str(inv) else inv
            deduplicated_inverse.append(keep)
            seen_inverses.add(p)
            seen_inverses.add(inv)
        else:
            deduplicated_inverse.append(p)

    # Drop properties that are an ancestor of another extractable property
    # (keep the most specialized form). E.g. if `hasArrangedPart` is in the
    # list and `hasPart` is its parent, drop `hasPart`.
    keep_set = set(deduplicated_inverse)
    final: list[URIRef] = []
    for p in deduplicated_inverse:
        # Is p the parent of any other extractable property? Then drop p.
        is_parent_of_kept = False
        for other in deduplicated_inverse:
            if other == p:
                continue
            ancestor = axioms.parent_property(ontology, other)
            while ancestor is not None:
                if ancestor == p:
                    is_parent_of_kept = True
                    break
                ancestor = axioms.parent_property(ontology, ancestor)
            if is_parent_of_kept:
                break
        if not is_parent_of_kept:
            final.append(p)

    return final


# ── LLM call (batch — one call per entity, returns all property values) ──

_STAGE2_BATCH_PROMPT = """\
You are extracting property values for the entity "{entity_label}" (a
{entity_class}) from its supporting quotes.

You may use ONLY the supporting context below. ONLY include properties
where the supporting quotes provide a clear value — omit properties with
no value rather than emitting null. Each value MUST cite a short verbatim
quote as evidence.

{document_context_block}
Supporting quotes (cited evidence for this entity):

{quotes_block}

Candidate properties (consider each; emit only the ones with a value):

{properties_block}

Known entities in this document (use the exact name in "value_entity" if
the property's value is one of these):

{known_entities_block}

Reply in JSON only:

{{
  "values": [
    {{
      "property":     "<property CURIE from the candidates above>",
      "value":        "<literal text>" or null,
      "value_entity": "<exact entity name>" or null,
      "evidence":     "<short verbatim quote ≤80 chars proving this value>"
    }}
  ]
}}

If a property's value is a literal (date, number, string), use "value".
If it's one of the known entities, use "value_entity" and leave "value" null.
Empty "values" list is valid if no candidate properties have values in the
supporting quotes.
"""


@dataclass
class PropertyResult:
    """Per-property result. Carries the original LLM payload + its evidence."""
    value:        str | None        = None
    value_entity: str | None        = None
    confidence:   float             = 0.0
    rationale:    str               = ""
    evidence:     str               = ""


@dataclass
class PropertyExtractionItem:
    """One item in a per-entity batch response: which property + its value."""
    prop:         URIRef
    result:       PropertyResult


def extract_properties_for_entity(
    entity:           ExtractedEntity,
    candidate_props:  list[URIRef],
    *,
    ontology:         Graph,
    document_context: str,
    known_entities:   list[ExtractedEntity],
    client:           LLMClient,
    model:            ModelConfig,
) -> list[PropertyExtractionItem]:
    """One LLM call returning all property values for *entity*.

    Replaces the per-property loop. Returns only properties for which the LLM
    found a value (omitted properties had no value in the supporting quotes
    — much cheaper than asking N times for null).
    """
    if not candidate_props:
        return []

    quotes_block = _format_quotes(entity)
    document_context_block = (
        f"Document context:\n{document_context}\n\n"
        if document_context else ""
    )
    known_entities_block = _format_known_entities(known_entities, ontology, exclude=entity)
    properties_block     = _format_candidate_properties(candidate_props, ontology)
    curie_to_prop        = {_curie_for_logging(p): p for p in candidate_props}

    prompt = _STAGE2_BATCH_PROMPT.format(
        entity_label           = entity.label,
        entity_class           = axioms.class_label(ontology, entity.type_uri),
        document_context_block = document_context_block,
        quotes_block           = quotes_block,
        properties_block       = properties_block,
        known_entities_block   = known_entities_block,
    )
    stage_label = f"part14/stage2/{entity.label}"
    meta = f"{model.model_id}  {len(candidate_props)} candidate properties"
    log_prompt(stage_label, prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response(stage_label, text, logger=logger, metadata=meta, as_json=True)
    return _parse_stage2_batch_response(text, curie_to_prop)


def _format_candidate_properties(props: list[URIRef], ontology: Graph) -> str:
    lines: list[str] = []
    for p in props:
        plabel = axioms.property_label(ontology, p)
        pdef   = axioms.property_definition(ontology, p) or "(no definition)"
        prange = axioms.range_of(ontology, p)
        rlabel = axioms.class_label(ontology, prange) if prange else "(any)"
        curie  = _curie_for_logging(p)
        # Truncate verbose definitions to keep the prompt small
        pdef_short = (pdef[:120] + "…") if len(pdef) > 120 else pdef
        lines.append(f"  - {curie} (range: {rlabel}) — {pdef_short}")
    return "\n".join(lines)


def _curie_for_logging(uri: URIRef) -> str:
    """Best-effort CURIE compaction. Falls back to the full URI when prefix
    unknown — same simple namespace map as bitmap.py uses."""
    s = str(uri)
    for ns, prefix in (
        ("http://rds.posccaesar.org/ontology/lis14/rdl/", "lis"),
        ("http://example.org/docgraph/meta#",          "dg"),
        ("http://www.w3.org/ns/oa#",                   "oa"),
        ("http://www.w3.org/ns/prov#",                 "prov"),
        ("http://www.w3.org/2002/07/owl#",             "owl"),
        ("http://www.w3.org/2000/01/rdf-schema#",      "rdfs"),
        ("http://www.w3.org/2001/XMLSchema#",          "xsd"),
    ):
        if s.startswith(ns):
            return f"{prefix}:{s[len(ns):]}"
    return f"<{s}>"


def _format_quotes(entity: ExtractedEntity) -> str:
    if not entity.evidence:
        return "(no supporting quotes)"
    parts = []
    for i, sel in enumerate(entity.evidence, 1):
        ctx = ""
        if sel.prefix or sel.suffix:
            ctx = f"  [context: ...{sel.prefix} __HERE__ {sel.suffix}...]"
        parts.append(f"[{i}] {sel.exact}{ctx}")
    return "\n".join(parts)


def _format_known_entities(
    entities: list[ExtractedEntity],
    ontology: Graph,
    *,
    exclude:  ExtractedEntity,
) -> str:
    other = [e for e in entities if e.uri != exclude.uri]
    if not other:
        return "(none)"
    lines = []
    for e in other:
        type_label = axioms.class_label(ontology, e.type_uri)
        lines.append(f'  - "{e.label}" ({type_label})')
    return "\n".join(lines)


def _parse_stage2_batch_response(
    text: str,
    curie_to_prop: dict[str, URIRef],
) -> list[PropertyExtractionItem]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("stage 2 batch: no JSON object in response %r", text[:200])
        return []
    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("stage 2 batch: JSON decode failed (%s)", exc)
        return []

    out: list[PropertyExtractionItem] = []
    for raw in obj.get("values", []) or []:
        if not isinstance(raw, dict):
            continue
        curie = str(raw.get("property", "")).strip()
        prop = curie_to_prop.get(curie)
        if prop is None:
            logger.warning("stage 2 batch: unknown property CURIE %r", curie)
            continue
        result = PropertyResult(
            value        = _str_or_none(raw.get("value")),
            value_entity = _str_or_none(raw.get("value_entity")),
            confidence   = 1.0,    # batch response doesn't carry per-property confidence
            rationale    = "",
            evidence     = str(raw.get("evidence", "") or "").strip(),
        )
        # Skip fully-empty items (LLM emitted but with no value)
        if result.value is None and result.value_entity is None:
            continue
        out.append(PropertyExtractionItem(prop=prop, result=result))
    return out


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


# ── Value coercion (literal vs entity URI) ────────────────────────────────

# Map common xsd: datatypes to a parser function (best-effort)
_LITERAL_PARSERS = {
    str(XSD.integer):           lambda s: Literal(int(s),    datatype=XSD.integer),
    str(XSD.nonNegativeInteger):lambda s: Literal(int(s),    datatype=XSD.nonNegativeInteger),
    str(XSD.decimal):           lambda s: Literal(s,         datatype=XSD.decimal),
    str(XSD.double):            lambda s: Literal(float(s),  datatype=XSD.double),
    str(XSD.boolean):           lambda s: Literal(s.lower() in ("true", "1", "yes")),
    str(XSD.date):              lambda s: Literal(s,         datatype=XSD.date),
    str(XSD.dateTime):          lambda s: Literal(s,         datatype=XSD.dateTime),
    str(XSD.string):            lambda s: Literal(s),
}


def coerce_literal(value: str, range_uri: URIRef | None) -> Literal:
    """Parse a string value into a typed Literal using the range's xsd: type
    when recognised, else fall back to a plain Literal."""
    if range_uri is not None:
        parser = _LITERAL_PARSERS.get(str(range_uri))
        if parser:
            try:
                return parser(value)
            except (ValueError, TypeError):
                pass
    return Literal(value)


def coerce_value(
    result:         PropertyResult,
    range_uri:      URIRef | None,
    known_entities: list[ExtractedEntity],
    *,
    rdl_resolvers:  list[RdlResolver] | None = None,
    confidence_floor: float = 0.5,
) -> URIRef | Literal | None:
    """Turn a PropertyResult into a Literal or URIRef for the triple.

    Resolution order:
      1. value_entity matches a known extracted entity → URIRef of that entity.
      2. value or value_entity → try each registered RDL resolver in order.
         First confident hit wins.
      3. value parses cleanly as the range's xsd: datatype → typed Literal.
      4. Fallback → plain string Literal.
      5. Both null → None (no triple).
    """
    if result.value_entity:
        match = next((e for e in known_entities if e.label == result.value_entity), None)
        if match:
            return match.uri

    if rdl_resolvers:
        probe = result.value or result.value_entity
        if probe:
            for resolver in rdl_resolvers:
                hit = resolver.resolve(probe, kind_hint=range_uri)
                if hit.uri is not None and hit.confidence >= confidence_floor:
                    return hit.uri

    if result.value is None and result.value_entity is None:
        return None
    raw = result.value if result.value is not None else result.value_entity
    return coerce_literal(raw, range_uri)


# ── Walker entry point ────────────────────────────────────────────────────

def walk_stage2(
    extracted_entities: list[ExtractedEntity],
    *,
    ontology:         Graph,
    document_context: str,
    client:           LLMClient,
    model:            ModelConfig,
    rdl_resolvers:    list[RdlResolver] | None = None,
    console=None,
) -> Graph:
    """Run stage 2 across every extracted entity. Returns triples to add to
    the source graph."""
    g = Graph()
    g.bind("dg",   DG)
    g.bind("lis",  LIS)
    g.bind("oa",   OA)
    g.bind("rdfs", RDFS)
    g.bind("xsd",  XSD)

    for entity in extracted_entities:
        properties = extractable_properties_for(entity.type_uri, ontology)
        if not properties:
            continue

        if console:
            console.print(f"  [dim]→ {entity.label}: batch ({len(properties)} candidates)[/dim]")

        items = extract_properties_for_entity(
            entity           = entity,
            candidate_props  = properties,
            ontology         = ontology,
            document_context = document_context,
            known_entities   = extracted_entities,
            client           = client,
            model            = model,
        )
        for item in items:
            range_uri = axioms.range_of(ontology, item.prop)
            value = coerce_value(
                item.result, range_uri, extracted_entities,
                rdl_resolvers=rdl_resolvers,
            )
            if value is not None:
                g.add((entity.uri, item.prop, value))

    return g


def infer_cross_entity_links(
    extracted_entities: list[ExtractedEntity],
    graph:              Graph,
    ontology:           Graph,
    *,
    console = None,
) -> Graph:
    """Deterministic post-pass that fills in missing class-ranged property
    triples by looking at supporting-quote co-occurrence.

    Motivation: the LLM sometimes extracts an entity AND a related entity but
    fails to connect them via the property that should link them — e.g.
    extracts `<invoice-total>` (ScalarQuantityDatum) with value "115.84" and
    `<eur>` (UnitOfMeasure) as a separate entity, but doesn't emit the
    `lis:datumUOM` link between them. The total's supporting quote contains
    "EUR 115,84", so the link is trivially recoverable.

    Algorithm (no LLM): for each entity, for each class-ranged extractable
    property whose domain matches the entity's type:
      1. Skip if any triple `(entity, property, *)` already exists.
      2. Iterate other extracted entities whose type satisfies the property's
         `rdfs:range`.
      3. If the other entity's label appears (case-insensitive, word-bounded)
         in the entity's supporting-quote text, emit the triple.

    Conservative: skips properties already populated, requires whole-word
    label match. Won't fire when the LLM already did its job.
    """
    import re

    new = Graph()
    new.bind("lis", LIS)
    inferred = 0

    label_to_entity_by_range: dict[URIRef, list[ExtractedEntity]] = {}
    # Pre-bucket entities by their type (cheap lookup by range later)
    for e in extracted_entities:
        label_to_entity_by_range.setdefault(e.type_uri, []).append(e)

    for entity in extracted_entities:
        if not entity.evidence:
            continue
        all_text = " ".join(s.exact for s in entity.evidence)
        if not all_text:
            continue

        props = extractable_properties_for(entity.type_uri, ontology)
        for prop in props:
            range_uri = axioms.range_of(ontology, prop)
            if range_uri is None:
                continue
            if not axioms.is_class_range(ontology, prop):
                continue
            # Skip if entity already has this property populated (idempotency)
            if any(graph.triples((entity.uri, prop, None))):
                continue
            # Find candidate target entities whose type satisfies the range
            candidates = [
                other for other in extracted_entities
                if other.uri != entity.uri
                and axioms.range_satisfied(ontology, [other.type_uri], prop)
            ]
            for other in candidates:
                if not other.label:
                    continue
                # Word-bounded case-insensitive match
                pattern = r"\b" + re.escape(other.label) + r"\b"
                if re.search(pattern, all_text, re.IGNORECASE):
                    new.add((entity.uri, prop, other.uri))
                    inferred += 1
                    if console:
                        console.print(
                            f"  [dim]inferred: {entity.label} "
                            f"{_local(prop)} {other.label}[/dim]"
                        )
                    # Only one inference per (entity, prop) — pick first match
                    break

    if console and inferred:
        console.print(f"  inferred [bold]{inferred}[/bold] cross-entity link(s) "
                      f"[dim]from quote co-occurrence[/dim]")
    return new


def resolve_deferred_references(
    deferred,                                       # list[DeferredReference]
    extracted_entities: list[ExtractedEntity],
    *,
    ontology:      Graph | None = None,
    rdl_resolvers: list[RdlResolver] | None = None,
    confidence_floor: float = 0.5,
    console=None,
) -> Graph:
    """After all branches have run (and entities of every class are known),
    bind deferred property values to URIs.

    Resolution per deferred ref:
      1. Match the cited name against any extracted entity's label → use its URI
         (subject to range validation against the loaded *ontology*).
      2. Try registered RDL resolvers in order → use the first confident hit.
      3. Otherwise emit the name as a plain Literal (so the value isn't lost).
    """
    g = Graph()
    g.bind("dg",   DG)
    g.bind("lis",  LIS)

    label_to_entity = {e.label: e for e in extracted_entities}
    resolved = unresolved = rejected_range = 0

    for ref in deferred:
        # 1. Known-entity match
        match = label_to_entity.get(ref.name)
        if match is not None:
            # Range validation — only when ontology was passed.
            if ontology is not None and not axioms.range_satisfied(
                ontology, [match.type_uri], ref.predicate
            ):
                logger.warning(
                    "deferred-ref: range violation %s obj=%s (a %s) — skipping",
                    ref.predicate, match.uri, match.type_uri,
                )
                rejected_range += 1
                continue
            g.add((ref.subject, ref.predicate, match.uri))
            resolved += 1
            continue

        # 2. RDL resolution
        bound = False
        if rdl_resolvers:
            for resolver in rdl_resolvers:
                hit = resolver.resolve(ref.name, kind_hint=ref.range_uri)
                if hit.uri is not None and hit.confidence >= confidence_floor:
                    g.add((ref.subject, ref.predicate, hit.uri))
                    resolved += 1
                    bound = True
                    break
        if bound:
            continue

        # 3. Fallback literal
        g.add((ref.subject, ref.predicate, Literal(ref.name)))
        unresolved += 1

    if console and (resolved or unresolved or rejected_range):
        msg = f"  resolved [bold]{resolved}[/bold] cross-entity refs"
        bits = []
        if unresolved:
            bits.append(f"{unresolved} fell back to literal")
        if rejected_range:
            bits.append(f"{rejected_range} rejected by range")
        if bits:
            msg += f" ([dim]{', '.join(bits)}[/dim])"
        console.print(msg)

    return g


def _local(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s
