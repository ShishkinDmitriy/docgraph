"""One-shot extraction — replaces the multi-call root + stage2 pipeline.

The mega-walker bundles everything into a SINGLE LLM call per document:

  1. Entity discovery + typing (Object/Aspect/Activity instances)
  2. Property value extraction (for each entity, including the binary
     properties that constitute template patterns — the dedicated
     SPARQL recognizer + LLM-confirm pass downstream finds the actual
     template invocations from these triples)
  3. Extension class proposals (when no existing class fits an entity)

Why one call instead of ~25:
  - Part 14 is small: ~50 classes, ~70 properties, ~3 templates fit easily
    in a single prompt.
  - Same context for the LLM → no cross-call inconsistency.
  - Cost goes down 60-70% per doc.
  - Latency goes down ~10x (one round trip vs ~25 sequential).

See docs/architecture/html-pipeline.md and the existing root_walker /
property_walker modules for the per-stage equivalents this replaces.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from src.extract_part14 import axioms
from src.extract_part14.ext_ontology import (
    EXT,
    LIS as LIS_NS,
    BLACKLISTED_ANCHORS,
    ExtClass,
    class_definitions_graph,
    extract_classes_from_graph,
    is_allowed_anchor,
    merge_proposals,
    normalize_slug,
    to_camel_case,
)
from src.extract_part14.property_walker import (
    _curie_for_logging,
    coerce_value,
    extractable_properties_for,
)
from src.extract_part14.rdl import RdlResolver
from src.extract_part14.root_walker import (
    _curie,
    _resolve_types,
    _subtree_text,
)
from src.extract_part14.walker import (
    DG,
    LIS,
    OA,
    EvidenceSelector,
    ExtractedEntity,
    mint_entity_uri,
    mint_fragment_uri,
)
from src.html_io import collapse_anchors
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)


# ── Prompt ───────────────────────────────────────────────────────────────

_MEGA_PROMPT = """\
You are extracting a knowledge graph from a document, end-to-end in one
call. The output is the document's complete Part 14 knowledge graph:
entities (with types, properties, evidence) and any new extension
classes proposed under LIS-14 superclasses.

== DOCUMENT ==

{document_context_block}

Markdown view (each meaningful element ends with `{{#id-N}}` and optionally
`{{#id-N .class-N}}` when coreferent mentions share a class):

\"\"\"
{markdown}
\"\"\"

== ONTOLOGY CLASS TREE ==

Full extractable class hierarchy. Use these CURIEs in entity `types`
fields. Multi-typing is fine — an entity can be e.g. both
FunctionalObject AND PhysicalObject (Part 14 §E.8).

{class_tree_block}

== EXISTING EXTENSION CLASSES ==

These ext: classes already exist in this project's accumulated graphs.
Prefer to REUSE one of these before proposing a new class:

{ext_classes_block}

== PROPOSING NEW EXTENSION CLASSES ==

When an entity doesn't fit any LIS-14 class or existing ext: class
naturally — and a more specific class would make the graph more
expressive — you may propose a new ext: class. Constraints:

  - The `anchor` is `rdfs:subClassOf` — pick any LIS-14 class from the
    class tree above EXCEPT these over-abstract roots, which are too
    generic to be a useful direct anchor:
{blacklisted_anchors_block}
    Land at the most specific class that still genuinely fits — e.g.
    `lis:InformationObject` for an Invoice (not `lis:Object`),
    `lis:Activity` for a DentalService (not `lis:Aspect`).
  - Provide `slug` (URI tail, PascalCase, e.g. "IBAN" or "BankAccount"),
    `anchor` (LIS-14 CURIE), `label` and `alt_labels` (BOTH in the same
    PascalCase style as the slug — "BankAccount" not "Bank Account",
    "Rechnung" not "rechnung"; preserve all-caps acronyms like "IBAN"
    or "BIC"), and `comment` (1-3 sentence natural-language definition,
    free-form prose).
  - Don't propose what already exists. Don't propose `ext:Person`
    (use `lis:Person`). Don't propose for purely-descriptive
    qualifiers ("informal", "draft") — those aren't classes, they're
    qualifiers and don't belong in the type system.
  - DO propose whenever an entity belongs to a coherent kind that
    LIS-14 doesn't have a class for — even "one-off" instances in
    this document. E.g. a phone number is `ext:PhoneNumber`, an
    email is `ext:EmailAddress`, an IBAN is `ext:IBAN`, a tax
    identifier is `ext:TaxIdentifier`. The whole point of ext: is to
    capture these recurring kinds; future docs that contain similar
    entities will REUSE the slug rather than re-proposing it.
  - Same conceptual class across docs MUST have the same `slug`
    (e.g. always "IBAN", never "IBAN_code" one time and "IBANcode"
    another). The "Existing extension classes" section above shows
    what's already proposed — reuse exactly when applicable.

If you propose a class with slug "IBAN" you must use it as a type:
the entity carries `types: ["ext:IBAN"]`. The class anchor handles
the placement in the hierarchy.

There is NO informal `type_hint` escape hatch — every entity must
carry one or more concrete CURIEs in `types`, either an existing
class or one you've proposed in `new_classes`. If you find yourself
wanting to "tag" an entity with a kind name, propose that kind as
an `ext:` class instead.

== PROPERTY CATALOG ==

Properties applicable to extracted entities. Each line shows the
property CURIE, its `domain` (which classes the property may attach
to — `any` means it applies universally), its `range` (what kind of
value it expects — a specific class CURIE, `Entity` for any-entity
object properties, or `Literal` for datatype properties), and a
one-line description. Only emit a property on an entity whose type
satisfies the domain.

{property_catalog_block}

For each entity, emit only properties for which the document provides
a clear value. Use `value` for literals (dates, numbers, strings),
`value_entity` for object-valued properties pointing at another
extracted entity (cite that entity's `name` exactly).

  IMPORTANT — Aspects are entities, not strings.
  When a property's range is an Aspect class (Quality, Function,
  Disposition, Role) — e.g. `lis:hasQuality`, `lis:hasFunction`,
  `lis:hasDisposition` — the value MUST be a separate entity that
  you also list under `entities`. DO NOT pass the description as a
  literal string.

  GOOD: cloak has hasQuality → entities also includes a Quality entity
        named "warmth" (typed lis:Quality), and the cloak's property
        is {{"property": "lis:hasQuality", "value_entity": "warmth"}}.
  BAD:  {{"property": "lis:hasQuality", "value": "warm, scarlet"}}
        — this loses the Aspect's identity and won't materialize.

  When in doubt, check the property catalog: any property with range
  Quality / Function / Disposition / Role / PhysicalQuantity needs a
  minted Aspect entity.

  Roles in particular: when an entity articulates a role in an activity
  (patient in a treatment, payer in a transaction, practitioner in a
  service), mint a SEPARATE entity typed `lis:Role` for the role
  itself, then connect three things:

    - `<role> rdf:type lis:Role` — the Role entity itself
    - `<role> lis:realizedIn <activity>` — links role to the activity
    - `<player> lis:hasRole <role>` — links the participant entity
      (the person/org who carries the role) to it

  All three triples together make the role fully connected. Without
  any one of them the role is an orphan. (Use the role's name as
  rdfs:label, e.g. "patient" / "practitioner" / "payer".)

== RESPONSE FORMAT ==

Reply with JSON only. Do NOT add prose before or after the JSON object;
put any explanations in the "notes" field.

{{
  "new_classes": [
    {{
      "slug":       "<URI tail>",
      "anchor":     "<lis: CURIE from the allowed anchors above>",
      "label":      "<canonical label>",
      "alt_labels": ["<synonym>", ...],
      "comment":    "<1-3 sentence definition>"
    }}
  ],
  "entities": [
    {{
      "name":        "<short canonical identifier>",
      "types":       ["<lis: or ext: curie>", ...],
      "evidence":    [{{"exact": "...", "anchor": "id-N"}}],
      "properties":  [
        {{"property": "<curie>", "value": "..." or null,
          "value_entity": "<exact entity name>" or null,
          "evidence": "<short verbatim quote>"}}
      ]
    }}
  ],
  "notes": "<optional commentary>"
}}

Rules:
  - Use ONLY class CURIEs from the class tree (or `ext:<slug>` after
    declaring the proposal in `new_classes`).
  - Use ONLY property CURIEs from the property catalog.
  - Every evidence entry must include an `anchor` matching a {{#id-N}}
    marker in the markdown.
  - Empty arrays are valid where applicable; don't invent content.
"""


# ── Prompt formatting helpers ────────────────────────────────────────────

def _format_class_tree(ontology: Graph) -> str:
    """Combined subtree text for all three roots, plus separator lines."""
    sections = []
    for root in (LIS.Object, LIS.Aspect, LIS.Activity):
        if not axioms.is_extractable(ontology, root):
            continue
        sections.append(f"### {axioms.class_label(ontology, root)} branch")
        sections.append(_subtree_text(root, ontology))
        sections.append("")
    return "\n".join(sections).rstrip()


def _format_ext_classes(classes: dict[str, ExtClass]) -> str:
    if not classes:
        return "  (none yet — propose new classes in `new_classes` if needed)"
    lines = []
    for slug in sorted(classes.keys()):
        c = classes[slug]
        anchor_curie = _curie(c.anchor)
        alts = f"  alt: {', '.join(c.alt_labels)}" if c.alt_labels else ""
        cmt  = f"  — {c.comment}" if c.comment else ""
        lines.append(f"  - ext:{slug}  (subClassOf {anchor_curie}, label \"{c.label}\"){alts}{cmt}")
    return "\n".join(lines)


def _format_blacklisted_anchors() -> str:
    return "\n".join(f"    - {_curie(uri)}" for uri in sorted(BLACKLISTED_ANCHORS, key=str))


def _format_property_catalog(ontology: Graph) -> str:
    """Render the full property catalog for all extractable classes —
    one consolidated list since the LLM is doing everything at once."""
    # Gather all extractable properties across the ontology
    seen: set[URIRef] = set()
    properties: list[URIRef] = []
    for root in (LIS.Object, LIS.Aspect, LIS.Activity):
        for cls in axioms.subclasses(ontology, root, direct=False) + [root]:
            for p in extractable_properties_for(cls, ontology):
                if p in seen:
                    continue
                seen.add(p)
                properties.append(p)

    lines = []
    for p in sorted(properties, key=str):
        pdef   = axioms.property_definition(ontology, p) or "(no definition)"
        pdef_short = (pdef[:140] + "…") if len(pdef) > 140 else pdef
        curie  = _curie_for_logging(p)
        # Domain — show the explicit class CURIEs the property attaches to,
        # or "any" when domain-less (universally applicable per LIS-14).
        domains = axioms.domains_of(ontology, p)
        dom_label = ", ".join(_curie(d) for d in domains) if domains else "any"
        # Range — explicit class when declared, otherwise "Entity" for object
        # properties or "Literal" for datatype properties (avoids the bare
        # "(any)" that lets the LLM pass strings to object properties).
        prange = axioms.range_of(ontology, p)
        if prange is not None:
            rng_label = _curie(prange)
        elif axioms.is_object_property(ontology, p):
            rng_label = "Entity"
        else:
            rng_label = "Literal"
        lines.append(f"  - {curie}  (domain: {dom_label};  range: {rng_label}) — {pdef_short}")
        # Surface scope notes if any (USE: lines from skos:scopeNote)
        for note in axioms.scope_notes(ontology, p):
            lines.append(f"      USE: {note}")
    return "\n".join(lines)


def _build_document_context(*, title: str, description: str) -> str:
    parts = [f"Title: {title!r}"]
    if description:
        parts.append(f"Description: {description}")
    return "\n".join(parts)


# ── LLM call ─────────────────────────────────────────────────────────────

def _call_llm(
    *,
    markdown:           str,
    document_title:     str,
    document_descr:     str,
    ontology:           Graph,
    existing_ext:       dict[str, ExtClass],
    client:             LLMClient,
    model:              ModelConfig,
) -> dict:
    prompt = _MEGA_PROMPT.format(
        document_context_block    = _build_document_context(
            title=document_title, description=document_descr,
        ),
        markdown                  = markdown,
        class_tree_block          = _format_class_tree(ontology),
        ext_classes_block         = _format_ext_classes(existing_ext),
        blacklisted_anchors_block = _format_blacklisted_anchors(),
        property_catalog_block    = _format_property_catalog(ontology),
    )
    meta = f"{model.model_id}  one-shot extraction"
    log_prompt("part14/mega", prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=16384,
    )
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response("part14/mega", text, logger=logger, metadata=meta, as_json=True)
    return _parse_response(text)


def _parse_response(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("mega: no JSON object in response %r", text[:200])
        return {}
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("mega: JSON decode failed (%s)", exc)
        return {}


# ── Entry point ──────────────────────────────────────────────────────────

@dataclass
class MegaResult:
    """Everything produced by one mega-walker call, ready to write."""
    graph:           Graph
    entities:        list[ExtractedEntity]
    new_ext_classes: list[ExtClass]
    notes:           str

    @property
    def class_definitions(self) -> Graph:
        """The new ext class declarations as a graph — merged into the
        main extract graph by the caller."""
        return class_definitions_graph(self.new_ext_classes)


def walk_mega(
    *,
    full_markdown:  str,
    document_title: str,
    document_descr: str,
    base_ns:        Namespace,
    md_source_uri:  URIRef,
    file_uri:       URIRef,
    ontology:       Graph,
    client:         LLMClient,
    model:          ModelConfig,
    id_to_class:    dict[str, str]      | None = None,
    class_to_ids:   dict[str, set[str]] | None = None,
    rdl_resolvers:  list[RdlResolver]   | None = None,
    console=None,
) -> MegaResult:
    """Run the mega-call and materialize the result into a graph.

    The graph holds: all extracted entities (with type triples, label,
    lis:representedBy fragment URIs), property triples, and the
    proposed-extension-class definitions (so the per-doc graph is
    self-contained).
    """
    id_to_class  = id_to_class  or {}
    class_to_ids = class_to_ids or {}

    if console:
        console.print("  [bold]mega-extraction[/bold] (one call: entities + properties)...")

    # Existing ext classes — only ones that have been PROMOTED (i.e. lifted
    # to the project ext: namespace by `docgraph promote` after meeting
    # the cross-doc threshold) are visible to the extracting LLM. Classes
    # that other docs proposed locally (dg:provenance "proposed-by-llm",
    # living in their proposing doc's `urn:docgraph:source:<slug>/`
    # namespace) stay invisible cross-doc — by design, so each doc's
    # extraction is independent and the dedup + promote steps decide what
    # gets shared. See ARCHITECTURE.md § ext-class lifecycle.
    existing_ext = {
        slug: cls for slug, cls in extract_classes_from_graph(ontology).items()
        if cls.provenance == "promoted"
    }

    payload = _call_llm(
        markdown        = full_markdown,
        document_title  = document_title,
        document_descr  = document_descr,
        ontology        = ontology,
        existing_ext    = existing_ext,
        client          = client,
        model           = model,
    )

    g = Graph()
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("ext",  EXT,  override=True, replace=True)
    g.bind("oa",   OA,   override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   base_ns, override=True, replace=True)
    g.bind("lis14tpl", Namespace("urn:docgraph:vocab:lis14tpl#"))

    # ── New ext class proposals ──
    raw_new = payload.get("new_classes", []) or []
    proposals = _parse_proposals(raw_new, source_uri=file_uri, ontology=ontology,
                                 base_ns=base_ns)
    merged_existing, newly_added = merge_proposals(existing_ext, proposals)
    # Add the NEW class declarations to the per-doc graph (self-contained).
    for triple in class_definitions_graph(newly_added):
        g.add(triple)
    if console and newly_added:
        names = ", ".join(c.slug for c in newly_added)
        console.print(f"  [dim]proposed {len(newly_added)} new ext class(es): {names}[/dim]")

    # Build the CURIE→URI resolver for type fields. Combines LIS-14
    # extractable classes and ext: classes (existing + just-proposed).
    all_classes: dict[str, URIRef] = {}
    for root in (LIS.Object, LIS.Aspect, LIS.Activity):
        for cls in axioms.subclasses(ontology, root, direct=False) + [root]:
            if axioms.is_extractable(ontology, cls):
                all_classes[_curie(cls)] = cls
    for slug, cls in merged_existing.items():
        all_classes[f"ext:{slug}"] = cls.uri

    # ── Entities ──
    raw_entities = payload.get("entities", []) or []
    extracted: list[ExtractedEntity] = []

    for inst in raw_entities:
        if not isinstance(inst, dict):
            continue
        name = (inst.get("name") or "").strip()
        if not name:
            continue

        types = _resolve_types(
            inst.get("types", []), all_classes,
            fallback_root=LIS.Object, log_label="mega",
        )
        if not types:
            continue

        entity_uri = mint_entity_uri(name, base_ns)
        if any(e.uri == entity_uri for e in extracted):
            logger.info("mega: duplicate URI %s for %r; skipping", entity_uri, name)
            continue

        for t in types:
            g.add((entity_uri, RDF.type, t))
        g.add((entity_uri, RDFS.label, Literal(name)))

        # Evidence → fragment URIs (with class-N collapse).
        evidence_list, cited_ids = _process_evidence(
            inst.get("evidence", []) or [],
        )
        for frag in collapse_anchors(cited_ids, id_to_class, class_to_ids):
            g.add((entity_uri, LIS.representedBy,
                   mint_fragment_uri(md_source_uri, frag)))

        new_entity = ExtractedEntity(
            uri      = entity_uri,
            type_uri = types[0],
            label    = name,
            evidence = evidence_list,
            types    = list(types),
        )
        extracted.append(new_entity)

    # ── Properties (per entity, applied to the graph) ──
    for inst in raw_entities:
        if not isinstance(inst, dict):
            continue
        name = (inst.get("name") or "").strip()
        entity = next((e for e in extracted if e.label == name), None)
        if entity is None:
            continue
        all_types = entity.types or [entity.type_uri]
        for raw_prop in inst.get("properties", []) or []:
            if not isinstance(raw_prop, dict):
                continue
            prop_curie = str(raw_prop.get("property", "")).strip()
            prop_uri = _resolve_property(prop_curie, ontology)
            if prop_uri is None:
                continue
            if not axioms.domain_satisfied(ontology, all_types, prop_uri):
                logger.warning("mega %s: domain violation %s — skipping", name, prop_curie)
                continue
            from src.extract_part14.property_walker import PropertyResult
            result = PropertyResult(
                value        = _str_or_none(raw_prop.get("value")),
                value_entity = _str_or_none(raw_prop.get("value_entity")),
                confidence   = 1.0,
                evidence     = str(raw_prop.get("evidence", "") or "").strip(),
            )
            if result.value is None and result.value_entity is None:
                continue
            range_uri = axioms.range_of(ontology, prop_uri)
            value = coerce_value(result, range_uri, extracted,
                                 rdl_resolvers=rdl_resolvers)
            if value is None:
                continue
            # Object properties demand entity-typed values. The LLM should
            # have minted a separate Aspect (Quality / Function / …) entity
            # and passed its name via `value_entity`. A literal here means
            # the LLM gave us a string description instead.
            if axioms.is_object_property(ontology, prop_uri) and isinstance(value, Literal):
                logger.warning(
                    "mega %s: %s expects an entity (object property), got literal %r — skipping; "
                    "the LLM should have minted a separate Aspect entity and referenced it via value_entity",
                    name, prop_curie, str(value),
                )
                continue
            # Range guard for entity-typed values
            if isinstance(value, URIRef):
                target = next((e for e in extracted if e.uri == value), None)
                if target is not None:
                    target_types = target.types or [target.type_uri]
                    if not axioms.range_satisfied(ontology, target_types, prop_uri):
                        logger.warning(
                            "mega %s: range violation %s → %s; skipping",
                            name, prop_curie, value,
                        )
                        continue
            g.add((entity.uri, prop_uri, value))

    # Template invocations are no longer asked-for in the mega prompt —
    # the dedicated SPARQL recognizer + batched-loop confirm pass (run by
    # the pipeline after this walker) finds them mechanically.

    notes = (payload.get("notes") or "").strip()
    if notes and console:
        console.print(f"    [dim italic]notes: {notes}[/dim italic]")

    return MegaResult(
        graph           = g,
        entities        = extracted,
        new_ext_classes = newly_added,
        notes           = notes,
    )


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_proposals(raw_new: list, *, source_uri: URIRef | None,
                     ontology: Graph, base_ns: Namespace) -> list[ExtClass]:
    """Parse `new_classes` entries from the LLM response into ExtClass.

    The new class lives in the doc's OWN namespace (`base_ns` —
    `urn:docgraph:source:<slug>/`), not the project-wide ext: namespace.
    So `Invoice` becomes `urn:docgraph:source:zahnrechnung2025/Invoice`,
    next to the doc's entity URIs. Promotion (via `docgraph promote`)
    later moves stable classes into the project ext: namespace.
    """
    out: list[ExtClass] = []
    for raw in raw_new:
        if not isinstance(raw, dict):
            continue
        slug = normalize_slug(str(raw.get("slug", "")).strip())
        if not slug:
            continue
        anchor_curie = str(raw.get("anchor", "")).strip()
        anchor_uri = _resolve_anchor(anchor_curie, ontology)
        if anchor_uri is None:
            logger.warning("mega: ext class %s has unresolved/forbidden anchor %r; skipping",
                           slug, anchor_curie)
            continue
        label = to_camel_case(str(raw.get("label", slug)).strip()) or slug
        # Alt labels share the slug's CamelCase style and dedupe against the
        # canonical label (so "Bank Account" + "BankAccount" → one entry).
        seen = {label}
        alt_labels: list[str] = []
        for a in (raw.get("alt_labels", []) or []):
            if not isinstance(a, str) or not a.strip():
                continue
            normalized = to_camel_case(a)
            if normalized and normalized not in seen:
                seen.add(normalized)
                alt_labels.append(normalized)
        comment = str(raw.get("comment", "")).strip()
        out.append(ExtClass(
            slug=slug, anchor=anchor_uri, label=label,
            alt_labels=alt_labels, comment=comment,
            provenance="proposed-by-llm",
            first_seen=source_uri,
            namespace=base_ns,
        ))
    return out


def _resolve_anchor(curie: str, ontology: Graph) -> URIRef | None:
    """Resolve a lis: CURIE to a URI for an ext: anchor.

    Accepts any LIS class except over-abstract roots (BLACKLISTED_ANCHORS)
    and only when the class is actually known in the ontology — protects
    against typos (e.g. "lis:Persn") landing as orphaned subClassOf links.
    """
    if not curie.startswith("lis:"):
        return None
    uri = URIRef(str(LIS) + curie[len("lis:"):])
    if not is_allowed_anchor(uri):
        return None
    if (uri, RDF.type, OWL.Class) not in ontology:
        return None
    return uri


def _resolve_property(curie: str, ontology: Graph) -> URIRef | None:
    """Resolve a property CURIE to a URI, verifying it's an extractable property."""
    if not curie.startswith("lis:"):
        return None
    uri = URIRef(str(LIS) + curie[len("lis:"):])
    # Verify it's actually a property in the ontology
    if axioms.is_extractable(ontology, uri):
        return uri
    return None


def _process_evidence(raw_evidence: list) -> tuple[list[EvidenceSelector], set[str]]:
    """Collect EvidenceSelectors + the set of cited anchor ids."""
    out: list[EvidenceSelector] = []
    cited: set[str] = set()
    for raw in raw_evidence:
        if not isinstance(raw, dict):
            continue
        exact = (raw.get("exact") or "").strip()
        anchor = (raw.get("anchor") or "").strip().lstrip("#")
        if not anchor:
            continue
        out.append(EvidenceSelector(exact=exact, anchor=anchor))
        cited.add(anchor)
    return out, cited


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
