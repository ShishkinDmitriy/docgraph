"""Pass A — root extraction.

Replaces the previous bitmap + per-branch walker. Three LLM calls, one per
disjoint LIS-14 root (Object, Aspect, Activity):

  - "Find every entity of root class X in this document"
  - Each entity may get MULTIPLE `rdf:type` triples (Part 14 §E.8 endorses
    stacking permanent classifications: an individual is FunctionalObject +
    PhysicalObject + Driver simultaneously).
  - Activity entities additionally yield participants + role hints.

Roles use Part 14's BFO-style Role pattern (§E.6) — POSC's LIS-14 already
declares `lis:Role`, `lis:hasRole`, `lis:realizedIn`. For each participant
that the LLM names a role for, we mint a `lis:Role` individual:

    <activity>      a lis:Activity ;
                    lis:hasParticipant <player> .
    <role/...>      a lis:Role ;
                    rdfs:label "patient" ;
                    lis:realizedIn <activity> ;
                    dg:typeHint "patient", "treated person" .
    <player>        lis:hasRole <role/...> .

The Role's *specific* subclass (e.g., `pca:Patient`) is left for enrich to
discover via RDL probes against the role's label + type hints.

Pass order: Object → Aspect → Activity. Earlier-pass entities appear in the
"Already extracted" block of later passes, so disjointness is respected and
Activity-participant cross-references resolve immediately by label when the
participant was already extracted in pass 1 or 2.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from src.extract_part14 import axioms
from src.extract_part14.walker import (
    DG, LIS, OA,
    EvidenceSelector,
    ExtractedEntity,
    _quote_local_name,
    mint_entity_uri,
    mint_quote,
    slug,
)
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class Role:
    """A reified role individual minted from an Activity's participant.

    Realized in a specific Activity. Initially typed as `lis:Role`; enrich
    may refine to a more specific Role subclass from RDL.
    """
    uri:        URIRef
    activity:   URIRef                         # lis:realizedIn target
    player:     URIRef                         # has lis:hasRole pointing at this role
    label:      str                            # short label, e.g., "patient"
    type_hints: list[str] = field(default_factory=list)   # candidate classes for enrichment
    evidence:   list[EvidenceSelector] = field(default_factory=list)


# ── Class subtree formatting for the prompt ────────────────────────────────

# Classes excluded from any root's class-subtree prompt block. lis:Role is
# minted by the Activity pass's participant role mechanism — extracting it
# directly under Aspect would produce parallel role individuals (one ersatz
# Aspect role + one reified Activity role for the same conceptual role).
# Roles only enter the graph through the role pattern; they're never
# extracted as standalone Aspect instances.
_SUBTREE_EXCLUDED: set[URIRef] = {LIS.Role}


def _subtree_text(root: URIRef, ontology: Graph) -> str:
    """Indented list of every extractable descendant of *root* (incl. root),
    one per line, with class CURIE + label + short definition.

    Showing the full descendant tree gives the LLM the candidate type set
    in a single block. Filters dg:plumbing classes and classes the pipeline
    handles via dedicated mechanisms (currently lis:Role).
    """
    visited: set[URIRef] = set()
    lines: list[str] = []

    def _walk(cls: URIRef, depth: int) -> None:
        if cls in visited:
            return
        visited.add(cls)
        if not axioms.is_extractable(ontology, cls):
            return
        if cls in _SUBTREE_EXCLUDED:
            return                              # also skips its descendants
        label = axioms.class_label(ontology, cls)
        defn  = axioms.class_definition(ontology, cls)
        defn_short = (defn[:80] + "…") if len(defn) > 80 else defn
        indent = "  " * depth
        curie  = _curie(cls)
        suffix = f" — {defn_short}" if defn_short else ""
        lines.append(f"{indent}- {curie}: {label}{suffix}")
        # skos:scopeNote → "USE:" guidance lines.
        # skos:example   → "EXAMPLE:" lines.
        # Local ontology overrides (dg-part14-alignments.ttl) can attach
        # these per class to correct LLM mis-use without touching prompts.
        hint_indent = indent + "    "
        for note in axioms.scope_notes(ontology, cls):
            lines.append(f"{hint_indent}USE: {note}")
        for ex in axioms.examples(ontology, cls):
            lines.append(f"{hint_indent}EXAMPLE: {ex}")
        for child in sorted(axioms.subclasses(ontology, cls, direct=True), key=str):
            _walk(child, depth + 1)

    _walk(root, 0)
    return "\n".join(lines)


# ── Prompts ────────────────────────────────────────────────────────────────

_OBJECT_ASPECT_PROMPT = """\
You are extracting every entity of root class "{root_label}" from a document.

Definition: {root_definition}

For each instance, return:
  - "name":     short canonical IDENTIFIER for this specific individual.
                Do NOT include the class name in "name" — the type is
                already captured in "types".
                  BAD:  "patient_role", "invoice_date", "service_function"
                  GOOD: "Dmitrii Shishkin" (a Person), "17.01.2025" (a date),
                        "tooth_cleaning" (the function itself), "1352" (the
                        invoice number), "EUR" (the currency).
                Pick something that distinguishes THIS individual from
                others of the same class.
  - "types":    list of class CURIEs from the tree below that ALL apply to
                this entity. Part 14 explicitly permits multiple permanent
                classifications (§E.8): an entity may be e.g. both
                FunctionalObject AND PhysicalObject AND Driver. List every
                applicable class — do not pick just one.
  - "evidence": one or more verbatim text spans that mention this entity.
                Each is {{exact, prefix, suffix}}:
                  - "exact"  = verbatim text (10–200 chars typical)
                  - "prefix" = ~30 chars immediately before
                  - "suffix" = ~30 chars immediately after
                Cite ALL spans where this entity is mentioned.
  - "type_hints": optional list of MORE-SPECIFIC class names (free text)
                  you'd suggest for this entity if asked to refine its
                  typing against a richer reference taxonomy. Used later
                  for RDL enrichment. Example: for a "monetary amount"
                  entity, hints might be ["Currency", "MonetaryUnit"].

Rules:
  - Use ONLY class CURIEs from the tree below in "types".
  - Empty "instances" list is valid (no entities of this root in the doc).
  - Do NOT re-extract entities listed in "Already extracted" — those are
    typed under another DISJOINT root.
{existing_block}
Class tree (root + every extractable descendant):
{subtree}

Document:
\"\"\"
{markdown}
\"\"\"

Reply in JSON only. Do NOT add prose before or after the JSON — if you
have anything to say about your choices, put it in the optional "notes"
field below.

{{
  "instances": [
    {{
      "name":       "...",
      "types":      ["<curie>", ...],
      "evidence":   [{{"exact": "...", "prefix": "...", "suffix": "..."}}],
      "type_hints": ["..."]
    }}
  ],
  "notes": "<optional: explanations, ambiguities, or reasons for empty results. Omit when there's nothing to add.>"
}}
"""

_ACTIVITY_PROMPT = """\
You are extracting every ACTIVITY from a document.

An lis:Activity (Part 14) is a happening: a process, event, service,
transaction, treatment, transformation — any concrete occurrence the
document describes.

For each Activity, return:
  - "name":     short canonical IDENTIFIER for this specific activity.
                Do NOT include the class name "Activity" or generic suffixes
                like "_process", "_event", "_action" — the type is already
                in "types".
                  BAD:  "tooth_cleaning_activity", "invoice_issuance_event"
                  GOOD: "professional tooth cleaning on 17.01.2025",
                        "invoice 1352 issuance"
                Use the most distinctive identifier (date, ID, descriptor).
  - "types":    list of class CURIEs from the tree below that ALL apply
                (multi-typing allowed; see Part 14 §E.8).
  - "evidence": verbatim text spans citing the activity ({{exact, prefix, suffix}}).
  - "participants": who/what takes part in this activity. For each:
      * "name":       exact name of the participant entity (will be
                      resolved to an already-extracted entity by label).
      * "role_hint":  OPTIONAL short role label the participant plays in
                      THIS activity (e.g., "patient", "practitioner",
                      "payer", "tool"). Omit when no specific role is
                      articulated.
      * "type_hints": OPTIONAL list of candidate specific role classes
                      (e.g., ["Patient"], ["Dentist", "HealthcareProvider"]).
                      Only meaningful when role_hint is set.
  - "type_hints": optional list of specific class names for this activity
                  itself, for RDL enrichment (e.g., ["DentalProcedure"]).

Rules:
  - Use ONLY class CURIEs from the tree below in "types".
  - Participants are entities already extracted under DISJOINT roots
    (Object / Aspect). Reference by name; we resolve to URIs.
  - Roles are OPTIONAL. Do not invent role labels for participants whose
    role is generic or implicit. A clear "the patient is X" justifies a
    role_hint; the mere fact of participation does not.
{existing_block}
Class tree (Activity + every extractable descendant):
{subtree}

Document:
\"\"\"
{markdown}
\"\"\"

Reply in JSON only. Do NOT add prose before or after the JSON — if you
have anything to say about your choices, put it in the optional "notes"
field below.

{{
  "instances": [
    {{
      "name":         "...",
      "types":        ["<curie>", ...],
      "evidence":     [{{"exact": "...", "prefix": "...", "suffix": "..."}}],
      "participants": [
        {{"name": "...", "role_hint": "...", "type_hints": ["..."]}}
      ],
      "type_hints":   ["..."]
    }}
  ],
  "notes": "<optional: explanations, ambiguities, or reasons for empty results. Omit when there's nothing to add.>"
}}
"""


def _format_existing(entities: list[ExtractedEntity]) -> str:
    if not entities:
        return ""
    lines = ["", "Already extracted (other disjoint roots — do not re-extract; reference by name where needed):"]
    for e in entities:
        ts = e.types if e.types else [e.type_uri]
        type_locals = ", ".join(_local(t) for t in ts)
        lines.append(f"  - {e.label} ({type_locals})")
    return "\n".join(lines) + "\n"


# ── LLM call dispatch ──────────────────────────────────────────────────────

def _extract_root(
    root:          URIRef,
    *,
    is_activity:   bool,
    full_markdown: str,
    existing:      list[ExtractedEntity],
    ontology:      Graph,
    client:        LLMClient,
    model:         ModelConfig,
) -> tuple[list[dict], str]:
    """One LLM call extracting entities of *root*. Returns (instances, notes)."""
    label   = axioms.class_label(ontology, root)
    defn    = axioms.class_definition(ontology, root) or "(no definition)"
    subtree = _subtree_text(root, ontology)
    template = _ACTIVITY_PROMPT if is_activity else _OBJECT_ASPECT_PROMPT

    prompt = template.format(
        root_label      = label,
        root_definition = defn,
        existing_block  = _format_existing(existing),
        subtree         = subtree,
        markdown        = full_markdown,
    )
    meta = f"{model.model_id}  root={label}"
    log_prompt(f"part14/root/{label}", prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=8192,
    )
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response(f"part14/root/{label}", text, logger=logger, metadata=meta, as_json=True)
    return _parse_instances(text)


def _parse_instances(text: str) -> tuple[list[dict], str]:
    """Parse the root extraction response. Returns (instances, notes).

    `notes` carries the LLM's optional commentary from the JSON's "notes"
    field — moves prose explanations into structured output instead of
    letting them trail the JSON.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("root_walker: no JSON object in response %r", text[:200])
        return [], ""
    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("root_walker: JSON decode failed (%s)", exc)
        return [], ""
    instances = obj.get("instances", [])
    if not isinstance(instances, list):
        instances = []
    notes = str(obj.get("notes", "") or "").strip()
    return instances, notes


# ── Walker entry point ─────────────────────────────────────────────────────

def walk_roots(
    full_markdown: str,
    *,
    base_ns:        Namespace,
    md_source_uri:  URIRef,
    ontology:       Graph,
    client:         LLMClient,
    model:          ModelConfig,
    console=None,
) -> tuple[Graph, list[ExtractedEntity], list[Role]]:
    """Run three-root extraction. Returns (graph, entities, roles).

    Order: Object → Aspect → Activity. Disjointness ensures each real-world
    entity appears in exactly one root. Activity participants resolve to
    URIs of already-extracted Object/Aspect entities by label match.
    """
    g = Graph()
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("oa",   OA,   override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   base_ns, override=True, replace=True)

    extracted: list[ExtractedEntity] = []
    roles:     list[Role] = []

    # Discover the three disjoint roots from the ontology. We hard-anchor on
    # LIS-14's Object/Aspect/Activity since those are the standard partition;
    # if a user-loaded ontology lifts the partition we'd want a more dynamic
    # discovery, but for Part 14 these are the normative roots.
    roots_in_order: list[tuple[URIRef, bool]] = [
        (LIS.Object,   False),
        (LIS.Aspect,   False),
        (LIS.Activity, True),
    ]

    for root_uri, is_activity in roots_in_order:
        if not axioms.is_extractable(ontology, root_uri):
            continue
        root_label = axioms.class_label(ontology, root_uri)
        if console:
            console.print(f"  extracting [bold]{root_label}[/bold] entities...")

        instances, notes = _extract_root(
            root_uri,
            is_activity   = is_activity,
            full_markdown = full_markdown,
            existing      = extracted,
            ontology      = ontology,
            client        = client,
            model         = model,
        )
        if notes and console:
            console.print(f"    [dim italic]notes: {notes}[/dim italic]")

        valid_descendants = _extractable_descendants(root_uri, ontology) | {root_uri}
        curie_to_uri = {_curie(c): c for c in valid_descendants}

        for inst in instances:
            name = (inst.get("name") or "").strip()
            if not name:
                continue

            types = _resolve_types(inst.get("types", []), curie_to_uri,
                                    fallback_root=root_uri, log_label=root_label)
            if not types:
                logger.warning("root_walker: no valid types for entity %r under %s — skipping",
                               name, root_label)
                continue

            # Single namespace: <base_ns><slug(name)>. The bound ex: prefix
            # then renders cleanly in Turtle. Multi-typed entities still get
            # one URI — the rdf:type list distinguishes them, not the URI path.
            entity_uri = mint_entity_uri(name, base_ns)

            # Skip duplicates (defensive — same name + same primary type
            # would mint the same URI, which the walker should already
            # prevent via the "already extracted" prompt block).
            if any(e.uri == entity_uri for e in extracted):
                logger.info("root_walker: duplicate URI %s for %r — skipping", entity_uri, name)
                continue

            for t in types:
                g.add((entity_uri, RDF.type, t))
            g.add((entity_uri, RDFS.label, Literal(name)))

            for hint in inst.get("type_hints", []) or []:
                if isinstance(hint, str) and hint.strip():
                    g.add((entity_uri, DG.typeHint, Literal(hint.strip())))

            evidence_list = _mint_evidence(
                inst.get("evidence", []) or [],
                entity_uri = entity_uri,
                graph = g, base_ns = base_ns, md_source_uri = md_source_uri,
            )

            new_entity = ExtractedEntity(
                uri      = entity_uri,
                type_uri = types[0],
                label    = name,
                evidence = evidence_list,
                types    = list(types),
            )
            extracted.append(new_entity)

            # Activity-specific: participants + roles
            if is_activity:
                _process_activity_participants(
                    inst.get("participants", []) or [],
                    activity_uri = entity_uri,
                    base_ns      = base_ns,
                    md_source_uri = md_source_uri,
                    extracted    = extracted,
                    graph        = g,
                    roles        = roles,
                )

        if console:
            new_count = sum(1 for e in extracted if e.type_uri in valid_descendants or e.type_uri == root_uri)
            console.print(f"    → [bold]{new_count}[/bold] {root_label} entit"
                          f"{'y' if new_count == 1 else 'ies'} so far")

    if console and roles:
        console.print(f"  [dim]minted {len(roles)} role individual(s) for enrichment[/dim]")

    return g, extracted, roles


# ── Participant / role processing (Activity branch) ────────────────────────

def _process_activity_participants(
    participants: list[dict],
    *,
    activity_uri:  URIRef,
    base_ns:       Namespace,
    md_source_uri: URIRef,
    extracted:     list[ExtractedEntity],
    graph:         Graph,
    roles:         list[Role],
) -> None:
    """For each named participant: link via lis:hasParticipant; if a
    role_hint is present, mint a lis:Role individual realized in the
    activity, and link the player → role via lis:hasRole."""
    for p in participants:
        if not isinstance(p, dict):
            continue
        player_name = (p.get("name") or "").strip()
        if not player_name:
            continue

        player_uri = _resolve_player_uri(player_name, extracted)
        if player_uri is None:
            # The LLM cited a participant we didn't extract under any root.
            # We could defer like the old walker did; for now log + skip.
            # Deferred resolution will pick this up if the entity gets
            # added later by another mechanism.
            logger.info("root_walker: participant %r not found among extracted entities",
                        player_name)
            continue

        graph.add((activity_uri, LIS.hasParticipant, player_uri))

        role_hint = (p.get("role_hint") or "").strip()
        if not role_hint:
            continue        # participant without a specific role — that's fine

        role_uri = _mint_role_uri(role_hint, activity_uri, base_ns)
        graph.add((role_uri, RDF.type,      LIS.Role))
        graph.add((role_uri, RDFS.label,    Literal(role_hint)))
        graph.add((role_uri, LIS.realizedIn, activity_uri))
        graph.add((player_uri, LIS.hasRole, role_uri))

        type_hints = [h.strip() for h in (p.get("type_hints") or []) if isinstance(h, str) and h.strip()]
        for hint in type_hints:
            graph.add((role_uri, DG.typeHint, Literal(hint)))

        roles.append(Role(
            uri        = role_uri,
            activity   = activity_uri,
            player     = player_uri,
            label      = role_hint,
            type_hints = type_hints,
        ))


def _resolve_player_uri(name: str, extracted: list[ExtractedEntity]) -> URIRef | None:
    """Case-insensitive label match against already-extracted entities."""
    target = name.casefold()
    for e in extracted:
        if e.label.casefold() == target:
            return e.uri
    return None


def _mint_role_uri(role_label: str, activity_uri: URIRef, base_ns: Namespace) -> URIRef:
    """Stable, prefix-friendly role URI: `<base_ns>role-<rolelabel>-in-<activity>`.

    Kept under the single base namespace (no `role/` sub-path) so rdflib
    serializes it with the bound `ex:` prefix instead of the long-form
    URI. The `role-` prefix at the start of the local name still makes
    role individuals visually distinguishable from regular entities.
    """
    role_slug     = slug(role_label, max_len=32)
    activity_slug = str(activity_uri).rsplit("/", 1)[-1][:48]
    return URIRef(base_ns[f"role-{role_slug}-in-{activity_slug}"])


# ── Evidence minting ───────────────────────────────────────────────────────

def _mint_evidence(
    raw_evidence: list,
    *,
    entity_uri:    URIRef,
    graph:         Graph,
    base_ns:       Namespace,
    md_source_uri: URIRef,
) -> list[EvidenceSelector]:
    out: list[EvidenceSelector] = []
    for raw_ev in raw_evidence:
        if not isinstance(raw_ev, dict):
            continue
        exact = (raw_ev.get("exact") or "").strip()
        if not exact:
            continue
        sel = EvidenceSelector(
            exact  = exact,
            prefix = (raw_ev.get("prefix") or "").strip(),
            suffix = (raw_ev.get("suffix") or "").strip(),
        )
        quote_uri = mint_quote(graph, sel, base_ns=base_ns, md_source_uri=md_source_uri)
        graph.add((entity_uri, LIS.representedBy, quote_uri))
        out.append(sel)
    return out


# ── Type resolution helpers ────────────────────────────────────────────────

def _resolve_types(
    raw_types:     list,
    curie_to_uri:  dict[str, URIRef],
    *,
    fallback_root: URIRef,
    log_label:     str,
) -> list[URIRef]:
    """Convert the LLM's CURIE list into ontology URIs. Drops unknown CURIEs.

    If nothing resolves, returns [fallback_root] so the entity at least
    gets its root type. Deduplicates while preserving order.
    """
    seen: set[URIRef] = set()
    out:  list[URIRef] = []
    for raw in raw_types:
        if not isinstance(raw, str):
            continue
        curie = raw.strip()
        uri = curie_to_uri.get(curie)
        if uri is None:
            logger.warning("root_walker[%s]: unknown class CURIE %r", log_label, curie)
            continue
        if uri in seen:
            continue
        seen.add(uri)
        out.append(uri)
    if not out:
        out = [fallback_root]
    return out


def _extractable_descendants(root: URIRef, ontology: Graph) -> set[URIRef]:
    """All extractable transitive subclasses of *root* (excluding plumbing)."""
    out: set[URIRef] = set()
    for c in axioms.subclasses(ontology, root, direct=False):
        if axioms.is_extractable(ontology, c):
            out.add(c)
    return out


# ── CURIE helpers (kept local to avoid coupling on walker internals) ───────

def _curie(uri: URIRef) -> str:
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


def _local(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s
