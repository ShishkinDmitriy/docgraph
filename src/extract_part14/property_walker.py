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
from src.extract_part14.walker import DG, LIS, OA, ExtractedEntity, slug
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig
from src.templates.expand import expand, materialize_lifted
from src.templates.loader import Template
from src.templates.registry import default_registry

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
You are extracting facts about the entity "{entity_label}" (a
{entity_class}) from its supporting quotes.

You have TWO mechanisms — fill them in this priority order:

  1. **Templates** (N-ary patterns) — multi-slot fact-patterns that
     describe how groups of properties co-occur on this entity's type.
     Prefer these: a single template invocation captures multiple
     related facts as one unit. Only emit a template invocation when
     all REQUIRED slots can be filled from the supporting quotes.

  2. **Properties** (binary patterns) — individual property values that
     aren't already covered by an emitted template invocation. Use these
     for one-off facts.

You may use ONLY the supporting context below. Each emitted fact MUST
cite a short verbatim quote as evidence.

{document_context_block}
Supporting quotes (cited evidence for this entity):

{quotes_block}

{templates_block}
Candidate properties (binary patterns — emit only those NOT already
covered by an emitted template invocation, and only when the supporting
quotes provide a clear value):

{properties_block}

Known entities in this document (use the exact name in "value_entity" or
in a slot binding if the value is one of these):

{known_entities_block}

Reply in JSON only. Do NOT add prose before or after the JSON object —
if you have anything to say about your choices, put it in the optional
"notes" field below.

{{
  "invocations": [
    {{
      "template": "<template CURIE from the templates section above>",
      "slots": {{
        "<slot-name>": "<bound entity name>" or "<literal>",
        ...
      }},
      "evidence": "<short verbatim quote ≤80 chars proving the invocation>"
    }}
  ],
  "values": [
    {{
      "property":     "<property CURIE from the candidates above>",
      "value":        "<literal text>" or null,
      "value_entity": "<exact entity name>" or null,
      "evidence":     "<short verbatim quote ≤80 chars proving this value>"
    }}
  ],
  "notes": "<optional: explanations, ambiguities, or reasons for empty results. Omit when there's nothing to add.>"
}}

For slot bindings: use literal strings for literal-typed slots (xsd:double,
xsd:string, etc.); use the EXACT name of a known entity for slot ranges
that are classes (the materializer resolves the name to a URI). If a
required slot's target entity doesn't exist yet in the "Known entities"
list AND the document supports extracting one, omit the invocation —
extraction of that target should happen in its own pass first.

Empty arrays are valid. Use "notes" to explain anything unusual.
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


@dataclass
class TemplateInvocation:
    """One filled N-ary template invocation from the LLM batch response.

    `slots` is name → raw string from the LLM (an entity name to be
    resolved against `known_entities`, or a literal value). The
    materializer coerces literals to typed forms and resolves entity
    names to URIs at expansion time.
    """
    template_uri: URIRef
    slots:        dict[str, str]
    evidence:     str = ""


def _applicable_templates(entity: ExtractedEntity) -> list[Template]:
    """All N-ary templates anchored on any of the entity's types.

    Looks up via tpl:subject on the cached registry. Multi-typing means
    one entity may have several anchored templates; deduplicate by URI."""
    seen: set[URIRef] = set()
    out: list[Template] = []
    reg = default_registry()
    for t in (entity.types or [entity.type_uri]):
        for tmpl in reg.by_subject(t):
            if tmpl.uri in seen:
                continue
            seen.add(tmpl.uri)
            out.append(tmpl)
    return out


def _format_templates_block(templates: list[Template]) -> str:
    """Render applicable templates for the stage2 prompt.

    Includes the natural-language definition + slot table with per-slot
    descriptions — the LLM uses these to decide whether the document
    supports the pattern and to bind slots correctly.
    """
    if not templates:
        return ""
    lines = ["Applicable templates (N-ary patterns — prefer these over individual properties):", ""]
    for t in templates:
        curie = _curie_for_logging(t.uri)
        label = t.label or str(t.uri).rsplit("#", 1)[-1]
        lines.append(f"  {curie} — {label}")
        if t.definition:
            lines.append(f'    Definition: "{t.definition}"')
        for s in t.slots:
            rng = _curie_for_logging(s.range) if s.range else "(any)"
            opt = " (OPTIONAL)" if s.min_count == 0 else " (REQUIRED)"
            lines.append(f"    - slot {s.name} : {rng}{opt}")
            desc = t.var_descriptions.get(s.name)
            if desc:
                for d_line in desc.splitlines():
                    lines.append(f"        {d_line}")
        lines.append("")
    return "\n".join(lines)


def extract_properties_for_entity(
    entity:           ExtractedEntity,
    candidate_props:  list[URIRef],
    *,
    ontology:         Graph,
    document_context: str,
    known_entities:   list[ExtractedEntity],
    client:           LLMClient,
    model:            ModelConfig,
) -> tuple[list[PropertyExtractionItem], list[TemplateInvocation], str]:
    """One LLM call returning property values + template invocations for *entity*.

    Returns (items, invocations, notes):
      - items: binary-property triples the LLM emitted (for properties NOT
        already covered by an invocation's lowered expansion).
      - invocations: N-ary template invocations the LLM filled in.
      - notes: LLM's optional commentary captured from the JSON response's
        "notes" field.

    Templates are prioritized in the prompt — the LLM is told to fill them
    first as multi-fact units, then fall through to individual properties
    for anything not captured by a template.
    """
    templates = _applicable_templates(entity)
    if not candidate_props and not templates:
        return [], [], ""

    quotes_block = _format_quotes(entity)
    document_context_block = (
        f"Document context:\n{document_context}\n\n"
        if document_context else ""
    )
    known_entities_block = _format_known_entities(known_entities, ontology, exclude=entity)
    properties_block     = _format_candidate_properties(candidate_props, ontology)
    templates_block      = _format_templates_block(templates)
    curie_to_prop        = {_curie_for_logging(p): p for p in candidate_props}
    curie_to_template    = {_curie_for_logging(t.uri): t for t in templates}

    prompt = _STAGE2_BATCH_PROMPT.format(
        entity_label           = entity.label,
        entity_class           = axioms.class_label(ontology, entity.type_uri),
        document_context_block = document_context_block,
        quotes_block           = quotes_block,
        templates_block        = templates_block,
        properties_block       = properties_block,
        known_entities_block   = known_entities_block,
    )
    stage_label = f"part14/stage2/{entity.label}"
    meta = f"{model.model_id}  {len(candidate_props)} props  {len(templates)} templates"
    log_prompt(stage_label, prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response(stage_label, text, logger=logger, metadata=meta, as_json=True)
    return _parse_stage2_batch_response(text, curie_to_prop, curie_to_template)


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
        # Optional behavioral guidance attached via skos:scopeNote /
        # skos:example. Used to correct LLM mis-application of specific
        # properties (e.g., datumValue's domain restriction).
        for note in axioms.scope_notes(ontology, p):
            lines.append(f"      USE: {note}")
        for ex in axioms.examples(ontology, p):
            lines.append(f"      EXAMPLE: {ex}")
    return "\n".join(lines)


def _curie_for_logging(uri: URIRef) -> str:
    """Best-effort CURIE compaction. Falls back to the full URI when prefix
    unknown — same simple namespace map as bitmap.py uses."""
    s = str(uri)
    for ns, prefix in (
        ("http://rds.posccaesar.org/ontology/lis14/rdl/", "lis"),
        ("http://example.org/docgraph/lis14tpl#",      "lis14tpl"),
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
    curie_to_template: dict[str, Template] | None = None,
) -> tuple[list[PropertyExtractionItem], list[TemplateInvocation], str]:
    """Parse the stage-2 batch response. Returns (items, invocations, notes).

    - `items`: binary property values (per the existing schema)
    - `invocations`: N-ary template invocations from the new `invocations`
      array. Each carries the template URI and a slot-name → raw-string map.
    - `notes`: LLM's optional commentary from the "notes" field.
    """
    curie_to_template = curie_to_template or {}
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("stage 2 batch: no JSON object in response %r", text[:200])
        return [], [], ""
    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("stage 2 batch: JSON decode failed (%s)", exc)
        return [], [], ""

    notes = str(obj.get("notes", "") or "").strip()

    # Parse invocations first — they have higher priority and influence
    # what gets emitted from the "values" array (dedupe happens at the
    # materializer level, not here).
    # Build a permissive lookup: try the CURIE as rendered, the bracket-
    # stripped variant, and the full URI form. Avoids losing invocations
    # when the LLM emits a slightly different shape than the prompt's CURIE.
    template_lookup: dict[str, Template] = {}
    for curie, t in curie_to_template.items():
        template_lookup[curie] = t
        template_lookup[curie.strip("<>")] = t
        template_lookup[str(t.uri)] = t
        template_lookup[f"<{t.uri}>"] = t

    invocations: list[TemplateInvocation] = []
    for raw in obj.get("invocations", []) or []:
        if not isinstance(raw, dict):
            continue
        curie = str(raw.get("template", "")).strip()
        tmpl = template_lookup.get(curie) or template_lookup.get(curie.strip("<>"))
        if tmpl is None:
            logger.warning("stage 2 batch: unknown template CURIE %r", curie)
            continue
        raw_slots = raw.get("slots", {}) or {}
        if not isinstance(raw_slots, dict):
            continue
        slots = {
            str(k): str(v).strip()
            for k, v in raw_slots.items()
            if v is not None and str(v).strip() != ""
        }
        if not slots:
            continue
        invocations.append(TemplateInvocation(
            template_uri = tmpl.uri,
            slots        = slots,
            evidence     = str(raw.get("evidence", "") or "").strip(),
        ))

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
    return out, invocations, notes


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
    base_ns:          Namespace | None = None,
    console=None,
) -> Graph:
    """Per-entity property extraction (Pass C of the three-pass model).

    Reads multi-typing off `entity.types` (falls back to [type_uri] for
    single-typed entities). Candidate properties = union of
    `extractable_properties_for(t)` across all of an entity's types.

    Validates domain (each property's rdfs:domain satisfied by at least
    one of the entity's types) and range (URIRef values typed compatibly
    with the property's declared range). Triples that fail either guard
    are dropped with a warning, not silently — same behavior as the old
    branch walker.
    """
    g = Graph()
    g.bind("dg",        DG)
    g.bind("lis",       LIS)
    g.bind("lis14tpl",  Namespace("http://example.org/docgraph/lis14tpl#"))
    g.bind("oa",        OA)
    g.bind("rdfs",      RDFS)
    g.bind("xsd",       XSD)

    for entity in extracted_entities:
        all_types = entity.types or [entity.type_uri]

        seen_props: set[URIRef] = set()
        properties: list[URIRef] = []
        for t in all_types:
            for p in extractable_properties_for(t, ontology):
                if p in seen_props:
                    continue
                seen_props.add(p)
                properties.append(p)

        if not properties:
            continue

        if console:
            console.print(f"  [dim]→ {entity.label}: batch ({len(properties)} candidates)[/dim]")

        items, invocations, notes = extract_properties_for_entity(
            entity           = entity,
            candidate_props  = properties,
            ontology         = ontology,
            document_context = document_context,
            known_entities   = extracted_entities,
            client           = client,
            model            = model,
        )
        if notes and console:
            console.print(f"    [dim italic]notes: {notes}[/dim italic]")

        # Materialize templates first — they take priority. Track the triples
        # the lowered expansion emits so the binary-property pass below can
        # skip duplicates without losing anything.
        covered: set = set()
        if invocations:
            inv_graph, covered = _materialize_invocations(
                invocations,
                extracted = extracted_entities,
                ontology  = ontology,
                base_ns   = base_ns,
                console   = console,
            )
            for triple in inv_graph:
                g.add(triple)

        for item in items:
            # Domain guard: at least one of the entity's types must satisfy
            # the property's rdfs:domain (domain-less props pass through).
            if not axioms.domain_satisfied(ontology, all_types, item.prop):
                logger.warning(
                    "stage2 %s: domain violation %s — entity types %s; skipping",
                    entity.label, _local(item.prop),
                    [_local(t) for t in all_types],
                )
                continue

            range_uri = axioms.range_of(ontology, item.prop)
            value = coerce_value(
                item.result, range_uri, extracted_entities,
                rdl_resolvers=rdl_resolvers,
            )
            if value is None:
                continue

            # Range guard: only enforced for URIRef values pointing at a
            # known entity. Literal values are validated by coerce_literal.
            if isinstance(value, URIRef):
                target = next((e for e in extracted_entities if e.uri == value), None)
                if target is not None:
                    target_types = target.types or [target.type_uri]
                    if not axioms.range_satisfied(ontology, target_types, item.prop):
                        logger.warning(
                            "stage2 %s: range violation %s → %s (a %s); skipping",
                            entity.label, _local(item.prop), value,
                            [_local(t) for t in target_types],
                        )
                        continue

            triple = (entity.uri, item.prop, value)
            if triple in covered:
                # Already emitted via a template invocation's lowered
                # expansion — skip to avoid asserting the same fact twice.
                continue
            g.add(triple)

    return g


def _materialize_invocations(
    invocations:  list[TemplateInvocation],
    *,
    extracted:    list[ExtractedEntity],
    ontology:     Graph,
    base_ns:      Namespace | None,
    console=None,
) -> tuple[Graph, set]:
    """Materialize each invocation into (a) a lifted fact-object capturing
    the invocation as a single typed entity with slot triples, plus (b) the
    lowered LIS-14 triples from the template's body.

    Returns (graph, covered_triples). `covered_triples` is the set of
    (s, p, o) tuples emitted by the lowered expansion — callers use it to
    skip duplicate triples when emitting binary property values.
    """
    g = Graph()
    covered: set = set()
    registry = default_registry()

    for inv in invocations:
        tmpl = registry.by_uri.get(inv.template_uri)
        if tmpl is None:
            logger.warning("invocation: unknown template %s", inv.template_uri)
            continue

        # Bind a sensible prefix for this template's slot namespace so the
        # serialized TTL uses `<slug>slot:datum` instead of `ns1:datum`.
        slot_ns = f"urn:tpl/{tmpl.slug}/slot/"
        g.bind(f"{tmpl.slug}-slot", Namespace(slot_ns), override=False)

        bindings = _resolve_slot_bindings(inv.slots, tmpl, extracted, ontology)
        if bindings is None:
            # A required slot couldn't be resolved — skip the invocation
            # rather than emit a half-formed fact.
            if console:
                tmpl_label = tmpl.label or _local(tmpl.uri)
                console.print(f"    [dim yellow]skipped invocation of {tmpl_label} "
                              f"(unresolved required slot)[/dim yellow]")
            continue

        try:
            lifted_graph  = materialize_lifted(tmpl, bindings, ext_ns=base_ns)
            lowered_graph = expand(tmpl, bindings, ext_ns=base_ns)
        except Exception as exc:                # pragma: no cover — guard against malformed bindings
            logger.warning("invocation: materialization failed for %s: %s",
                           inv.template_uri, exc)
            continue

        for s, p, o in lifted_graph:
            if _is_omit_sentinel(s) or _is_omit_sentinel(p) or _is_omit_sentinel(o):
                continue
            g.add((s, p, o))
        for s, p, o in lowered_graph:
            if _is_omit_sentinel(s) or _is_omit_sentinel(p) or _is_omit_sentinel(o):
                continue
            g.add((s, p, o))
            covered.add((s, p, o))

    return g, covered


_OMIT_NS = "urn:docgraph/omit#"
"""Sentinel namespace for omitted-optional-slot bindings.

When the LLM legitimately leaves an optional slot empty, we still have to
hand the expander SOME binding (else it mints a stray intermediate URI
that pollutes the graph). We bind the slot to a sentinel URI in this
namespace, then post-filter the expanded graph to drop any triple that
mentions a sentinel — effectively erasing triples that reference the
omitted slot."""


def _is_omit_sentinel(term) -> bool:
    return isinstance(term, URIRef) and str(term).startswith(_OMIT_NS)


def _resolve_slot_bindings(
    raw_slots: dict[str, str],
    template:  Template,
    extracted: list[ExtractedEntity],
    ontology:  Graph,
) -> dict[str, object] | None:
    """Resolve LLM-emitted slot strings into URI / typed-Literal bindings.

    Per slot:
      - Literal range (xsd:double, xsd:string, ...) → typed Literal via coerce_literal.
      - Class range (owl:Class) → resolve the raw as a CURIE using the
        template's source @prefix bindings, then verify the class actually
        exists in the loaded ontology — guards against the LLM inventing
        plausible-sounding class names (e.g., "MonetaryQuantityDatum") that
        the upstream ontology doesn't declare.
      - Instance range (any other URI class) → case-insensitive label match
        against `extracted`. If not found, try CURIE resolution.

    Returns the bindings dict, or None if any REQUIRED slot couldn't be
    resolved — invocation should be skipped rather than emit a half-formed fact.

    Omitted optional slots are bound to a sentinel URI in `_OMIT_NS`; the
    caller filters triples involving sentinels out of the expanded graph.
    """
    OWL_CLASS = URIRef("http://www.w3.org/2002/07/owl#Class")
    bindings: dict[str, object] = {}
    prefixes = template.prefixes or {}

    for slot in template.slots:
        raw = raw_slots.get(slot.name, "").strip()
        if not raw:
            if slot.min_count == 0:
                # Bind to a sentinel; the materializer drops triples that
                # involve it after expansion.
                bindings[slot.name] = URIRef(f"{_OMIT_NS}{slot.name}")
                continue
            return None         # required slot missing

        if slot.is_literal:
            bindings[slot.name] = coerce_literal(raw, slot.range)
            continue

        if slot.range == OWL_CLASS:
            class_uri = _curie_to_uri(raw, prefixes)
            if class_uri is not None and _class_declared(ontology, class_uri):
                bindings[slot.name] = class_uri
                continue
            if class_uri is not None:
                logger.info(
                    "invocation: slot %r on %s — class %s isn't declared in the "
                    "loaded ontology; LLM may have invented it. Treating slot as omitted.",
                    slot.name, template.uri, class_uri,
                )
            # Either unresolvable CURIE or class doesn't exist — fall
            # through to the same handling as instance-slot fallback.
        else:
            # Instance slot: try label match in extracted entities first
            match = next(
                (e for e in extracted if e.label.casefold() == raw.casefold()),
                None,
            )
            if match is not None:
                bindings[slot.name] = match.uri
                continue

            # Fallback: maybe the LLM gave a CURIE for an external entity
            # (e.g. a unit-of-measure from a reference data library).
            resolved = _curie_to_uri(raw, prefixes)
            if resolved is not None:
                bindings[slot.name] = resolved
                continue

        if slot.min_count == 0:
            # Optional and unresolved — drop via sentinel (post-filtered).
            bindings[slot.name] = URIRef(f"{_OMIT_NS}{slot.name}")
            continue
        logger.info(
            "invocation: required slot %r on %s — couldn't resolve %r; skipping",
            slot.name, template.uri, raw,
        )
        return None

    return bindings


def _class_declared(ontology: Graph, class_uri: URIRef) -> bool:
    """True if *class_uri* appears as an owl:Class (or rdfs:Class) in the
    loaded ontology — even with no further axioms. Guards against the LLM
    inventing class names that look syntactically valid but don't exist."""
    OWL_CLASS = URIRef("http://www.w3.org/2002/07/owl#Class")
    RDFS_CLASS = URIRef("http://www.w3.org/2000/01/rdf-schema#Class")
    if (class_uri, RDF.type, OWL_CLASS) in ontology:
        return True
    if (class_uri, RDF.type, RDFS_CLASS) in ontology:
        return True
    # Also accept any URI that's the subject of an rdfs:subClassOf triple —
    # some ontologies declare classes implicitly via subClassOf only.
    RDFS_SUB_CLASS_OF = URIRef("http://www.w3.org/2000/01/rdf-schema#subClassOf")
    if any(ontology.triples((class_uri, RDFS_SUB_CLASS_OF, None))):
        return True
    return False


def _curie_to_uri(raw: str, prefixes: dict[str, str]) -> URIRef | None:
    """Expand a CURIE using the template's source @prefix bindings, or
    return the raw value as a URI if it already looks like one."""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("urn:"):
        return URIRef(raw)
    if ":" in raw:
        prefix, _, local = raw.partition(":")
        ns = prefixes.get(prefix)
        if ns:
            return URIRef(ns + local)
    return None


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


def _local(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s
