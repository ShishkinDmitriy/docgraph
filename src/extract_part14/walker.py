"""M2 stage 1 — branch walker for the part14 pipeline.

Iterates over the upper ontology's top-level classes (derived at runtime),
runs one LLM call per branch to extract entities of that class, and mints
each entity's supporting quotes top-down using oa:TextQuoteSelector.

Branches run sequentially. Each branch's prompt receives entities found by
prior branches as context, with disjoint-incompatible types flagged so the
LLM doesn't re-extract them.

Stage 2 (per-entity property extraction) lives in property_walker.py and
is invoked separately after stage 1 completes.

See docs/architecture/extraction.md § "Pass 2 — Stage 1: entity extraction
per branch" for the full algorithm.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import PROV, RDF, RDFS, XSD

from src.extract_part14 import axioms
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

DG  = Namespace("http://example.org/docgraph/meta#")
LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")
OA  = Namespace("http://www.w3.org/ns/oa#")

logger = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class EvidenceSelector:
    exact:  str
    prefix: str = ""
    suffix: str = ""


@dataclass
class ExtractedEntity:
    uri:        URIRef
    type_uri:   URIRef
    label:      str
    evidence:   list[EvidenceSelector] = field(default_factory=list)


# ── Quote minting (top-down — quotes only for cited evidence) ──────────────

def _quote_local_name(exact: str) -> str:
    """Deterministic SHA-1 of the exact text — yields cross-source dedup."""
    return "quote-" + hashlib.sha1(exact.encode("utf-8")).hexdigest()[:12]


def mint_quote(
    g: Graph,
    selector: EvidenceSelector,
    *,
    base_ns: Namespace,
    md_source_uri: URIRef,
) -> URIRef:
    """Mint a dg:Quote with an oa:TextQuoteSelector pointing into the
    markdown source. Idempotent — same text → same URI; calling twice adds
    the same triples, no duplication."""
    q_uri = URIRef(base_ns[_quote_local_name(selector.exact)])
    g.add((q_uri, RDF.type, DG.Quote))
    g.add((q_uri, RDF.type, LIS.InformationObject))
    g.add((q_uri, OA.hasSource, md_source_uri))

    sel_node = BNode()
    g.add((q_uri, OA.hasSelector, sel_node))
    g.add((sel_node, RDF.type, OA.TextQuoteSelector))
    g.add((sel_node, OA.exact, Literal(selector.exact)))
    if selector.prefix:
        g.add((sel_node, OA.prefix, Literal(selector.prefix)))
    if selector.suffix:
        g.add((sel_node, OA.suffix, Literal(selector.suffix)))

    return q_uri


# ── Entity URI minting ─────────────────────────────────────────────────────

_SLUG_RX = re.compile(r"[^a-z0-9]+")


def _entity_local(branch_label: str, name: str) -> str:
    branch_slug = _SLUG_RX.sub("-", branch_label.lower()).strip("-")[:32]
    name_slug   = _SLUG_RX.sub("-", name.lower()).strip("-")[:48]
    if not name_slug:
        name_slug = "anon-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{branch_slug}/{name_slug}"


def mint_entity_uri(branch_label: str, entity_name: str, base_ns: Namespace) -> URIRef:
    return URIRef(base_ns[_entity_local(branch_label, entity_name)])


# ── Per-branch combined extraction ────────────────────────────────────────
# Each branch's LLM call returns BOTH the entities AND their property values
# in one shot, replacing the older two-stage walker (find entities → batch
# property-extract per entity). Properties whose values reference other
# entities not yet extracted are emitted as `value_entity` names; a final
# resolution pass binds them to URIs after all branches finish.

_BRANCH_PROMPT = """\
You are extracting instances of "{class_label}" from a document AND, for
each instance, the values of its properties that the document supports.

Class definition: {class_definition}

For each instance, return:
  - "name": a short canonical name (used to mint a stable URI)
  - "evidence": one or more verbatim text spans that mention this entity.
                Each is {{exact, prefix, suffix}}:
                  - "exact"  = verbatim text (10–200 chars typical)
                  - "prefix" = ~30 chars immediately before
                  - "suffix" = ~30 chars immediately after
                Cite ALL spans that mention the entity (coreference matters).
  - "properties": list of property values you can support from the document.
                  ONLY include properties whose value the document actually
                  contains — omit the rest, do NOT emit them with null.

Each property entry:
    {{
      "property":     "<property CURIE from the candidate list below>",
      "value":        "<literal text>" or null,
      "value_entity": "<exact entity name>" or null,
      "evidence":     "<short verbatim quote ≤80 chars proving the value>"
    }}

  - Use "value" for literal values (dates, numbers, free-form strings).
  - Use "value_entity" for object-valued properties — provide the EXACT
    name of the referenced entity. Use the same name even if that entity
    isn't in "Already extracted" yet — it may be discovered in another
    branch. If the entity IS listed below, copy its name verbatim.

Candidate properties for {class_label} (consider each; emit only the ones
with a value found in the document):

{properties_block}

Rules:
  - Empty "instances" list is valid (no entities of this class in the document).
  - Do NOT re-emit entities listed in "Already extracted" — reference them by
    name in property values if relevant.
  - Do NOT extract entities listed in "Excluded" — they're typed incompatibly
    with {class_label}.
{existing_block}{excluded_block}
Document:
\"\"\"
{markdown}
\"\"\"

Reply in JSON only, no prose:

{{
  "instances": [
    {{
      "name": "...",
      "evidence": [{{"exact": "...", "prefix": "...", "suffix": "..."}}],
      "properties": [
        {{"property": "<curie>", "value": "...", "value_entity": null, "evidence": "..."}}
      ]
    }}
  ]
}}
"""


def _format_existing(
    entities:       list[ExtractedEntity],
    current_branch: URIRef,
    ontology:       Graph,
) -> str:
    """Render the 'Already extracted' block for the LLM prompt.

    Splits entries into two groups so the LLM treats them differently:

    1. **Subclass entries** — entities whose type is a strict subclass of
       *current_branch*. These represent the SAME real-world thing as any
       generic instance the LLM might consider extracting, and the more
       specific typing wins. Strong "do not re-emit" language.

    2. **Other compatible entries** — entities of unrelated (but not
       disjoint) types. Reference these by name in property values; don't
       re-extract.

    This split is what prevents the duplicate-across-branches problem when
    the bitmap selects both a parent and its subclass (e.g.,
    `lis:QuantityDatum` + `lis:ScalarQuantityDatum`).
    """
    if not entities:
        return ""

    subclass_entries: list[ExtractedEntity] = []
    other_entries:    list[ExtractedEntity] = []
    for e in entities:
        if e.type_uri == current_branch:
            other_entries.append(e)        # walker dedup should prevent this; keep safe
        elif current_branch in axioms.superclasses(ontology, e.type_uri, direct=False):
            subclass_entries.append(e)     # current_branch is a SUPER of e
        else:
            other_entries.append(e)

    lines: list[str] = [""]
    branch_label = axioms.class_label(ontology, current_branch)

    if subclass_entries:
        lines.append(
            f"Already extracted at MORE SPECIFIC subclasses of {branch_label} — "
            f"DO NOT re-emit these as plain {branch_label} (their specific "
            f"typing is preferred):"
        )
        for e in subclass_entries:
            type_local = str(e.type_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            lines.append(f'  - {e.label} ({type_local})')

    if other_entries:
        if subclass_entries:
            lines.append("")
        lines.append("Already extracted (compatible types — do not re-emit; reference by name):")
        for e in other_entries:
            type_local = str(e.type_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            lines.append(f'  - {e.label} ({type_local})')

    return "\n".join(lines) + "\n"


def _format_excluded(excluded: list[ExtractedEntity]) -> str:
    if not excluded:
        return ""
    lines = ["", "Excluded (incompatible type — do not re-extract):"]
    for e in excluded:
        type_local = str(e.type_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        lines.append(f'  - {e.label} ({type_local})')
    return "\n".join(lines) + "\n"


def extract_branch_combined(
    branch:        URIRef,
    full_markdown: str,
    *,
    existing:      list[ExtractedEntity],
    excluded:      list[ExtractedEntity],
    candidate_props: list[URIRef],
    ontology:      Graph,
    client:        LLMClient,
    model:         ModelConfig,
) -> list[dict]:
    """One LLM call to extract instances of *branch* AND their property values.

    Returns the parsed JSON instances list (raw — caller mints URIs / quotes
    and resolves property values). Returns [] on parse failure or empty result.
    """
    label      = axioms.class_label(ontology, branch)
    definition = axioms.class_definition(ontology, branch) or "(no definition available)"
    properties_block = _format_candidate_properties(candidate_props, ontology)

    prompt = _BRANCH_PROMPT.format(
        class_label      = label,
        class_definition = definition,
        properties_block = properties_block,
        existing_block   = _format_existing(existing, branch, ontology),
        excluded_block   = _format_excluded(excluded),
        markdown         = full_markdown,
    )
    meta = f"{model.model_id}  branch={label}  {len(candidate_props)} candidate props"
    log_prompt(f"part14/branch/{label}", prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response(f"part14/branch/{label}", text, logger=logger, metadata=meta, as_json=True)
    return _parse_branch_response(text)


def _format_candidate_properties(props: list[URIRef], ontology: Graph) -> str:
    if not props:
        return "  (none — only entity discovery for this branch)"
    lines = []
    for p in props:
        plabel = axioms.property_label(ontology, p)
        pdef   = axioms.property_definition(ontology, p) or "(no definition)"
        prange = axioms.range_of(ontology, p)
        rlabel = axioms.class_label(ontology, prange) if prange else "(any)"
        curie  = _curie(p)
        pdef_short = (pdef[:120] + "…") if len(pdef) > 120 else pdef
        lines.append(f"  - {curie} (range: {rlabel}) — {pdef_short}")
    return "\n".join(lines)


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


def _parse_branch_response(text: str) -> list[dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("branch: no JSON object in response %r", text[:200])
        return []
    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("branch: JSON decode failed (%s)", exc)
        return []
    instances = obj.get("instances", [])
    if not isinstance(instances, list):
        return []
    return instances


# ── Branch ordering ────────────────────────────────────────────────────────

def _sort_by_specificity(branches: list[URIRef], ontology: Graph) -> list[URIRef]:
    """Sort *branches* so subclasses are processed before their superclasses.

    Topological order using `rdfs:subClassOf` over the selected set: a class
    with N ancestors also in the set goes BEFORE classes with fewer. Concrete
    case: when both `lis:QuantityDatum` and `lis:ScalarQuantityDatum` are
    selected, ScalarQuantityDatum (1 ancestor in set) runs first, so when
    QuantityDatum's prompt assembles its 'Already extracted' block it can
    show the SQD entities and tell the LLM not to re-extract them at the
    parent level.

    Among unrelated branches (siblings, no subclass relationship between
    them), input order is preserved — only specificity overrides caller
    intent. This matters because callers may have ordered branches for
    reasons unrelated to the class hierarchy (e.g., extracting Person
    before Activity so participant references resolve immediately).
    """
    branch_set = set(branches)

    def ancestors_in_set(b: URIRef) -> int:
        return sum(
            1 for sup in axioms.superclasses(ontology, b, direct=False)
            if sup in branch_set
        )

    indexed = list(enumerate(branches))
    indexed.sort(key=lambda pair: (-ancestors_in_set(pair[1]), pair[0]))
    return [b for _, b in indexed]


# ── Walker entry point ─────────────────────────────────────────────────────

@dataclass
class DeferredReference:
    """A property triple whose object is an entity not yet extracted at the
    time the branch ran. Resolved in a final pass after all branches finish."""
    subject:   URIRef
    predicate: URIRef
    name:      str           # the entity name as the LLM cited it
    range_uri: URIRef | None = None


def walk_branches(
    full_markdown: str,
    *,
    base_ns:        Namespace,
    md_source_uri:  URIRef,
    ontology:       Graph,
    client:         LLMClient,
    model:          ModelConfig,
    branches:       list[URIRef] | None = None,
    branch_namespace: str = str(LIS),
    console=None,
) -> tuple[Graph, list[ExtractedEntity], list[DeferredReference]]:
    """Run combined entity-discovery + property-extraction per branch.

    Returns:
      g                — the extraction graph (typed entities, evidence quotes,
                         literal property values, intra-batch URI references)
      extracted        — list of ExtractedEntity, useful for downstream
                         resolution and for callers that want to inspect
                         what was found
      deferred         — list of DeferredReference for property values whose
                         entity wasn't extracted yet (to be resolved by
                         `property_walker.resolve_deferred_references` after
                         all branches finish)

    Branches run sequentially; each pass excludes entities incompatible
    with that branch's class via the disjointness lookup.
    """
    g = Graph()
    g.bind("dg",   DG,   override=True, replace=True)
    g.bind("lis",  LIS,  override=True, replace=True)
    g.bind("oa",   OA,   override=True, replace=True)
    g.bind("rdfs", RDFS, override=True, replace=True)
    g.bind("xsd",  XSD,  override=True, replace=True)
    g.bind("ex",   base_ns, override=True, replace=True)
    extracted: list[ExtractedEntity] = []
    deferred:  list[DeferredReference] = []

    if branches is None:
        branches = axioms.effective_branches(ontology, namespace=branch_namespace)

    branches = _sort_by_specificity(branches, ontology)
    if console:
        order_labels = ", ".join(axioms.class_label(ontology, b) for b in branches)
        console.print(f"  [dim]branch order (specific → generic): {order_labels}[/dim]")

    # Avoid circular imports — these are stage-2 helpers reused for value
    # coercion when the LLM gives a literal value.
    from src.extract_part14.property_walker import (
        extractable_properties_for,
        coerce_literal,
    )

    for branch in branches:
        if not axioms.is_extractable(ontology, branch):
            if console:
                console.print(f"  [dim]skip {axioms.class_label(ontology, branch)} (dg:extractable false)[/dim]")
            continue

        disjoint_set = axioms.disjoint_with(ontology, branch)
        excluded   = [e for e in extracted if e.type_uri in disjoint_set]
        candidates = [e for e in extracted if e.type_uri not in disjoint_set]

        label = axioms.class_label(ontology, branch)
        candidate_props = extractable_properties_for(branch, ontology)
        curie_to_prop   = {_curie(p): p for p in candidate_props}

        if console:
            console.print(f"  extracting [bold]{label}[/bold] "
                          f"([dim]{len(candidates)} carried over, "
                          f"{len(excluded)} excluded, "
                          f"{len(candidate_props)} props[/dim])...")

        instances = extract_branch_combined(
            branch          = branch,
            full_markdown   = full_markdown,
            existing        = candidates,
            excluded        = excluded,
            candidate_props = candidate_props,
            ontology        = ontology,
            client          = client,
            model           = model,
        )

        for inst in instances:
            name = (inst.get("name") or "").strip()
            if not name:
                continue
            entity_uri = mint_entity_uri(label, name, base_ns)

            if any(e.uri == entity_uri for e in extracted):
                logger.info("branch: skipping duplicate URI %s for %s", entity_uri, label)
                continue

            # Type + label
            g.add((entity_uri, RDF.type, branch))
            g.add((entity_uri, RDFS.label, Literal(name)))

            # Evidence quotes
            evidence_list: list[EvidenceSelector] = []
            for raw_ev in inst.get("evidence", []) or []:
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
                quote_uri = mint_quote(g, sel, base_ns=base_ns, md_source_uri=md_source_uri)
                g.add((entity_uri, LIS.representedBy, quote_uri))
                evidence_list.append(sel)

            new_entity = ExtractedEntity(
                uri      = entity_uri,
                type_uri = branch,
                label    = name,
                evidence = evidence_list,
            )
            extracted.append(new_entity)

            # Property values from the combined response
            for raw_prop in inst.get("properties", []) or []:
                if not isinstance(raw_prop, dict):
                    continue
                prop_curie = str(raw_prop.get("property", "")).strip()
                prop_uri = curie_to_prop.get(prop_curie)
                if prop_uri is None:
                    logger.warning("branch %s: unknown property CURIE %r", label, prop_curie)
                    continue

                # Domain validation: reject if the predicate's rdfs:domain isn't
                # satisfied by the entity's type.
                if not axioms.domain_satisfied(ontology, [branch], prop_uri):
                    logger.warning(
                        "branch %s: domain violation %r subj=%s — skipping triple",
                        label, prop_curie, branch,
                    )
                    continue

                range_uri = axioms.range_of(ontology, prop_uri)

                # Try value_entity first (cross-entity reference)
                value_entity = (raw_prop.get("value_entity") or "").strip() or None
                if value_entity:
                    match = next(
                        (e for e in extracted if e.label == value_entity), None
                    )
                    if match:
                        # Range validation: reject if the resolved entity's type
                        # doesn't satisfy the predicate's rdfs:range. Catches
                        # `<org> lis:representedBy <person>` (range InformationObject).
                        if not axioms.range_satisfied(ontology, [match.type_uri], prop_uri):
                            logger.warning(
                                "branch %s: range violation %r obj=%s (a %s) — skipping triple",
                                label, prop_curie, match.uri, match.type_uri,
                            )
                            continue
                        g.add((entity_uri, prop_uri, match.uri))
                    else:
                        # Defer until all branches have run; range_uri carried
                        # along for validation at resolution time.
                        deferred.append(DeferredReference(
                            subject=entity_uri, predicate=prop_uri,
                            name=value_entity, range_uri=range_uri,
                        ))
                    continue

                # Literal value
                value = (raw_prop.get("value") or "").strip() or None
                if value:
                    # Reject literal-where-class-range-expected. The LLM can
                    # confuse "value" (literal) with "value_entity" (URI) when
                    # the property's range is a class — catch that here so we
                    # don't pollute the graph with bare-string-where-URI.
                    if axioms.is_class_range(ontology, prop_uri):
                        logger.warning(
                            "branch %s: literal where class range expected: %r value=%r — skipping triple",
                            label, prop_curie, value,
                        )
                        continue
                    g.add((entity_uri, prop_uri, coerce_literal(value, range_uri)))

        if console:
            new_count = sum(1 for e in extracted if e.type_uri == branch)
            new_props = sum(1 for d in deferred if d.subject in {e.uri for e in extracted if e.type_uri == branch})
            console.print(f"    → [bold]{new_count}[/bold] {label} "
                          f"entit{'y' if new_count == 1 else 'ies'}, "
                          f"[dim]{new_props} deferred refs[/dim]")

    return g, extracted, deferred


# Backwards-compatibility shim — older callers used `walk_stage1`. The new
# combined walker subsumes it.
def walk_stage1(*args, **kwargs):
    g, extracted, _deferred = walk_branches(*args, **kwargs)
    return g, extracted
