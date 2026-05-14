"""One-shot extraction — replaces the multi-call root + stage2 pipeline.

The mega-walker bundles everything into a SINGLE LLM call per document:

  1. Subject classification (what is this document ABOUT)
  2. Entity discovery + typing (Object/Aspect/Activity instances)
  3. Property value extraction (for each entity)
  4. Template invocations (N-ary patterns)
  5. Role minting (Activity participants with role hints)
  6. Extension class proposals (when no existing class fits an entity)

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
from rdflib.namespace import RDF, RDFS, XSD

from src.extract_part14 import axioms
from src.extract_part14.ext_ontology import (
    EXT,
    LIS as LIS_NS,
    ALLOWED_ANCHORS,
    ExtClass,
    class_definitions_graph,
    extract_classes_from_graph,
    merge_proposals,
    normalize_slug,
)
from src.extract_part14.property_walker import (
    _curie_for_logging,
    _materialize_invocations,
    _resolve_slot_bindings,
    coerce_value,
    extractable_properties_for,
    TemplateInvocation,
)
from src.extract_part14.rdl import RdlResolver
from src.extract_part14.root_walker import (
    Role,
    _curie,
    _local,
    _mint_role_uri,
    _process_activity_participants,
    _render_template_inline,
    _resolve_player_uri,
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
from src.templates.registry import default_registry

logger = logging.getLogger(__name__)


# ── Prompt ───────────────────────────────────────────────────────────────

_MEGA_PROMPT = """\
You are extracting a knowledge graph from a document, end-to-end in one
call. The output is the document's complete Part 14 knowledge graph:
subject classification, entities (with types, properties, evidence,
template invocations, role hints), and any new extension classes
proposed under stable LIS-14 superclasses.

== DOCUMENT ==

{document_context_block}

Markdown view (each meaningful element ends with `{{#id-N}}` and optionally
`{{#id-N .class-N}}` when coreferent mentions share a class):

\"\"\"
{markdown}
\"\"\"

== ROOT CLASSES (subject classification) ==

The document is FUNDAMENTALLY about one or more of these top-level
classes (Part 14's three disjoint roots and selected sub-branches):

{subject_candidates_block}

Pick the 1–3 that best describe what the document is about (mutually
compatible — you can choose more than one if the document genuinely
covers them).

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

  - Each proposed class must be a direct `rdfs:subClassOf` one of the
    following whitelisted LIS-14 anchors:
{allowed_anchors_block}
  - Provide `slug` (URI tail, kebab- or PascalCase), `anchor` (LIS-14
    CURIE), `label` (canonical short name), `alt_labels` (synonyms /
    aliases, can include surface forms used in the document), and
    `comment` (1-3 sentence definition explaining what the class
    represents).
  - Use proposals sparingly — only when an existing class genuinely
    doesn't fit. Don't propose `ext:Person` (use `lis:Person`).
    Don't propose for one-off entities.
  - Same conceptual class across docs should have the same `slug`
    (e.g. always "IBAN", never "IBAN_code" one time and "IBANcode"
    another). The "Existing extension classes" section above shows
    what's already proposed — reuse exactly when applicable.

If you propose a class with slug "IBAN" you must use it as a type:
the entity carries `types: ["ext:IBAN"]`. The class anchor handles
the placement in the hierarchy.

== PROPERTY CATALOG ==

Properties applicable to extracted entities. Each line shows the
property CURIE, its rdfs:domain (which classes it can attach to —
empty means universal), rdfs:range (what kind of value it expects),
and a one-line description.

{property_catalog_block}

For each entity, emit only properties for which the document provides
a clear value. Use `value` for literals (dates, numbers, strings),
`value_entity` for object-valued properties pointing at another
extracted entity (cite that entity's `name` exactly).

== TEMPLATES (N-ary patterns) ==

Multi-slot patterns that bundle related facts. When ALL the required
slots of a template can be filled from the document, prefer emitting
the template invocation over a list of individual properties — it's
more structured and easier to validate downstream.

{templates_block}

== ROLES ==

Activity participants may carry a role hint when their role IS
articulated in the document (patient, practitioner, payer, ...). Roles
are reified as their own entities (`lis:Role`), realized in the
activity they participate in. Each participant entry under an activity:

  - `name`: the participant entity's exact name (must match a name
            you emit elsewhere in `entities`).
  - `role_hint`: optional short role label.
  - `type_hints`: optional candidate specific role classes.

== RESPONSE FORMAT ==

Reply with JSON only. Do NOT add prose before or after the JSON object;
put any explanations in the "notes" field.

{{
  "subject": {{
    "classes": ["<curie>", ...],
    "confidence": <0.0..1.0>,
    "rationale": "<one short sentence>"
  }},
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
      "type_hints":  ["..."],
      "properties":  [
        {{"property": "<curie>", "value": "..." or null,
          "value_entity": "<exact entity name>" or null,
          "evidence": "<short verbatim quote>"}}
      ],
      "invocations": [
        {{"template": "<curie>",
          "slots":    {{"<slot-name>": "<entity name or literal>"}},
          "evidence": "<quote>"}}
      ]
    }}
  ],
  "activities": [
    {{
      "name":     "<activity entity name — matches one in `entities`>",
      "participants": [
        {{"name": "<entity name>", "role_hint": "...", "type_hints": ["..."]}}
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

def _format_subject_candidates(ontology: Graph) -> str:
    """Same shape as classify.subject_candidates — labels + definitions
    for the top-level classes the document might be ABOUT."""
    from src.extract_part14.classify import subject_candidates
    candidates = subject_candidates(ontology)
    lines = []
    for c in candidates:
        defn_short = (c.description[:120] + "…") if len(c.description) > 120 else c.description
        suffix = f" — {defn_short}" if defn_short else ""
        lines.append(f"  - {_curie(c.uri)}: {c.label}{suffix}")
    return "\n".join(lines)


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


def _format_allowed_anchors() -> str:
    return "\n".join(f"    - {_curie(uri)}" for uri in sorted(ALLOWED_ANCHORS, key=str))


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
        plabel = axioms.property_label(ontology, p)
        pdef   = axioms.property_definition(ontology, p) or "(no definition)"
        pdef_short = (pdef[:140] + "…") if len(pdef) > 140 else pdef
        prange = axioms.range_of(ontology, p)
        rlabel = axioms.class_label(ontology, prange) if prange else "(any)"
        curie  = _curie_for_logging(p)
        lines.append(f"  - {curie} (range: {rlabel}) — {pdef_short}")
        # Surface scope notes if any (USE: lines from skos:scopeNote)
        for note in axioms.scope_notes(ontology, p):
            lines.append(f"      USE: {note}")
    return "\n".join(lines)


def _format_templates(ontology: Graph) -> str:
    """All registered templates, regardless of anchor. The LLM picks
    which apply to which extracted entity."""
    reg = default_registry()
    templates = list(reg.all())
    if not templates:
        return "  (no templates registered)"
    lines: list[str] = []
    for t in templates:
        for line in _render_template_inline(t):
            lines.append(f"  {line}")
        lines.append("")
    return "\n".join(lines).rstrip()


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
        subject_candidates_block  = _format_subject_candidates(ontology),
        class_tree_block          = _format_class_tree(ontology),
        ext_classes_block         = _format_ext_classes(existing_ext),
        allowed_anchors_block     = _format_allowed_anchors(),
        property_catalog_block    = _format_property_catalog(ontology),
        templates_block           = _format_templates(ontology),
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
    roles:           list[Role]
    new_ext_classes: list[ExtClass]
    subject:         dict
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

    The graph holds: subject classification triples, all extracted
    entities (with type triples, label, dg:typeHint, lis:representedBy
    fragment URIs), property triples, template invocation lifted+lowered
    triples, role individuals, and the proposed-extension-class
    definitions (so the per-doc graph is self-contained).
    """
    id_to_class  = id_to_class  or {}
    class_to_ids = class_to_ids or {}

    if console:
        console.print("  [bold]mega-extraction[/bold] (one call: subject + "
                      "entities + properties + invocations + roles)...")

    # Existing ext classes — visible from the loader's union view of the
    # project so the LLM can reuse before proposing.
    existing_ext = extract_classes_from_graph(ontology)

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
    g.bind("lis14tpl", Namespace("http://example.org/docgraph/lis14tpl#"))

    # ── New ext class proposals ──
    raw_new = payload.get("new_classes", []) or []
    proposals = _parse_proposals(raw_new, source_uri=file_uri)
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

    # ── Subject classification ──
    subject = payload.get("subject", {}) or {}
    subject_classes = _resolve_subject(subject.get("classes", []), all_classes)
    for s in subject_classes:
        g.add((file_uri, DG.isAbout, s))
    confidence = subject.get("confidence")
    if isinstance(confidence, (int, float)):
        g.add((file_uri, DG.subjectConfidence,
               Literal(round(float(confidence), 3), datatype=XSD.decimal)))
    rationale = subject.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        g.add((file_uri, DG.reason, Literal(rationale.strip())))
    if console and subject_classes:
        labels = ", ".join(_local(c) for c in subject_classes)
        console.print(f"  subject: [bold]{labels}[/bold]")

    # ── Entities ──
    raw_entities = payload.get("entities", []) or []
    extracted: list[ExtractedEntity] = []
    activity_uris: dict[str, URIRef] = {}     # name → URI (used by Activities pass)

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
        for hint in inst.get("type_hints", []) or []:
            if isinstance(hint, str) and hint.strip():
                g.add((entity_uri, DG.typeHint, Literal(hint.strip())))

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
            type_hints = [str(h).strip() for h in (inst.get("type_hints", []) or [])
                          if isinstance(h, str) and h.strip()],
        )
        extracted.append(new_entity)
        # Track Activity URIs for the activities-participants pass below
        if any(_is_activity(t, ontology) for t in types):
            activity_uris[name] = entity_uri

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

    # ── Template invocations ──
    covered_lowered: set = set()
    for inst in raw_entities:
        if not isinstance(inst, dict):
            continue
        name = (inst.get("name") or "").strip()
        entity = next((e for e in extracted if e.label == name), None)
        if entity is None:
            continue
        raw_invs = inst.get("invocations", []) or []
        if not raw_invs:
            continue
        reg = default_registry()
        invs: list[TemplateInvocation] = []
        for raw in raw_invs:
            if not isinstance(raw, dict):
                continue
            curie = str(raw.get("template", "")).strip()
            # Permissive lookup: bare CURIE, bracketed, or full URI
            tmpl = None
            for u, t in [(curie, None)] + [(f"<{c.uri}>", c) for c in reg.all()]:
                pass
            # Lookup via the registry's full-URI map
            for t in reg.all():
                if curie in (_curie_for_logging(t.uri), f"<{t.uri}>", str(t.uri)):
                    tmpl = t
                    break
            if tmpl is None:
                logger.warning("mega: unknown template %r", curie)
                continue
            slots_raw = raw.get("slots", {}) or {}
            if not isinstance(slots_raw, dict):
                continue
            slots = {str(k): str(v).strip() for k, v in slots_raw.items()
                     if v is not None and str(v).strip()}
            if not slots:
                continue
            invs.append(TemplateInvocation(template_uri=tmpl.uri, slots=slots,
                                            evidence=str(raw.get("evidence", "") or "")))
        if invs:
            inv_g, covered = _materialize_invocations(
                invs, extracted=extracted, ontology=ontology,
                base_ns=base_ns, console=console,
            )
            for triple in inv_g:
                g.add(triple)
            covered_lowered.update(covered)

    # ── Activity-driven roles ──
    roles: list[Role] = []
    for act in (payload.get("activities", []) or []):
        if not isinstance(act, dict):
            continue
        a_name = (act.get("name") or "").strip()
        a_uri  = activity_uris.get(a_name)
        if a_uri is None:
            continue
        _process_activity_participants(
            act.get("participants", []) or [],
            activity_uri=a_uri, base_ns=base_ns,
            md_source_uri=md_source_uri, extracted=extracted,
            graph=g, roles=roles,
        )

    notes = (payload.get("notes") or "").strip()
    if notes and console:
        console.print(f"    [dim italic]notes: {notes}[/dim italic]")

    return MegaResult(
        graph           = g,
        entities        = extracted,
        roles           = roles,
        new_ext_classes = newly_added,
        subject         = subject,
        notes           = notes,
    )


# ── Helpers ──────────────────────────────────────────────────────────────

def _parse_proposals(raw_new: list, *, source_uri: URIRef | None) -> list[ExtClass]:
    """Parse `new_classes` entries from the LLM response into ExtClass."""
    out: list[ExtClass] = []
    for raw in raw_new:
        if not isinstance(raw, dict):
            continue
        slug = normalize_slug(str(raw.get("slug", "")).strip())
        if not slug:
            continue
        anchor_curie = str(raw.get("anchor", "")).strip()
        anchor_uri = _resolve_anchor(anchor_curie)
        if anchor_uri is None:
            logger.warning("mega: ext class %s has unresolved/forbidden anchor %r; skipping",
                           slug, anchor_curie)
            continue
        label = str(raw.get("label", slug)).strip() or slug
        alt_labels = [str(a).strip() for a in (raw.get("alt_labels", []) or [])
                      if isinstance(a, str) and a.strip()]
        comment = str(raw.get("comment", "")).strip()
        out.append(ExtClass(
            slug=slug, anchor=anchor_uri, label=label,
            alt_labels=alt_labels, comment=comment,
            provenance="proposed-by-llm",
            first_seen=source_uri,
        ))
    return out


def _resolve_anchor(curie: str) -> URIRef | None:
    """Resolve a lis: CURIE to a URI, checking it's in ALLOWED_ANCHORS."""
    if not curie.startswith("lis:"):
        return None
    uri = URIRef(str(LIS) + curie[len("lis:"):])
    return uri if uri in ALLOWED_ANCHORS else None


def _resolve_subject(raw_classes: list, all_classes: dict[str, URIRef]) -> list[URIRef]:
    out: list[URIRef] = []
    for raw in raw_classes:
        if not isinstance(raw, str):
            continue
        u = all_classes.get(raw.strip())
        if u is None:
            continue
        out.append(u)
    return out


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


def _is_activity(class_uri: URIRef, ontology: Graph) -> bool:
    """True if class_uri is lis:Activity or a (transitive) subclass."""
    if class_uri == LIS.Activity:
        return True
    return LIS.Activity in axioms.superclasses(ontology, class_uri, direct=False)


def _str_or_none(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None
