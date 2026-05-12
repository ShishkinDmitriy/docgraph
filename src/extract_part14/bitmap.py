"""Bitmap-based class selection — replaces threshold-based descent.

Instead of using a structural heuristic (number of subclasses) to decide which
classes are worth extracting from a document, ask the LLM directly: "for each
extractable class in the loaded ontology, does this document contain at least
one entity best typed as THIS class (not as a more specific subclass)?"

The result is a non-aggregating bitmap: Person=true and Organism=false are
independent — they're judged on their own merits per document.

This is one extra LLM call per document (small — bitmap fits in a few hundred
tokens) that typically saves N stage-1 LLM calls (often >50%) by skipping
branches that have no instances in the document.

Falls back to threshold-based descent (`axioms.effective_branches`) when no
LLM client is available — keeps the offline path working.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from rdflib import Graph, URIRef

from src.extract_part14 import axioms
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)


@dataclass
class ClassEvidence:
    cls:      URIRef
    evidence: str          # short verbatim quote the LLM cited as proof


@dataclass
class BitmapResult:
    selected:  list[URIRef]                          # final selection (LLM picks + range-coupling additions)
    evidence:  dict[URIRef, str] = field(default_factory=dict)  # class → first cited evidence
    rationale: str = ""
    coupling_added: list[URIRef] = field(default_factory=list)  # classes added by range coupling, not LLM
    started:   datetime | None = None
    ended:     datetime | None = None


# ── Class collection ───────────────────────────────────────────────────────

def collect_extractable_classes(
    ontology: Graph,
    namespace: str | None = None,
    *,
    max_depth: int = 6,
) -> list[URIRef]:
    """All extractable classes reachable from top-level classes, transitively
    via rdfs:subClassOf, filtered through `is_extractable` (so dg:plumbing
    classes are skipped).

    *namespace* filters the initial top-level scan (so PROV-O / OA / dg
    plumbing classes don't seed the walk). Descent crosses namespaces — a
    user-loaded `inv:Invoice rdfs:subClassOf lis:InformationObject` would be
    included.
    """
    visited: set[URIRef] = set()
    out: list[URIRef] = []

    def _walk(cls: URIRef, depth: int) -> None:
        if cls in visited or depth >= max_depth:
            return
        visited.add(cls)
        if axioms.is_extractable(ontology, cls):
            out.append(cls)
        for child in axioms.subclasses(ontology, cls, direct=True):
            _walk(child, depth + 1)

    for top in axioms.top_level_classes(ontology, namespace=namespace):
        if not axioms.is_extractable(ontology, top):
            # Even if the top is non-extractable (e.g. a dg root with all
            # plumbing children), descend in case it has extractable subclasses
            for child in axioms.subclasses(ontology, top, direct=True):
                _walk(child, 1)
        else:
            _walk(top, 0)

    return sorted(out, key=str)


# ── Hierarchy formatting for the prompt ───────────────────────────────────

def format_hierarchy(
    ontology: Graph,
    classes: list[URIRef],
) -> str:
    """Render the class set as an indented hierarchical tree.

    Only classes in *classes* appear; subclass-of relationships outside the
    set are flattened (e.g., if Person is in the set but Organism isn't, Person
    appears at the depth of its first ancestor that IS in the set).
    """
    class_set = set(classes)

    # Build parent-of map within the set: each class's parent is its CLOSEST
    # super-class that's also in the set. We BFS upward via direct supers
    # rather than walking transitive supers + sorting, because rdflib's
    # `superclasses(direct=False)` doesn't preserve closest-first order.
    parent_in_set: dict[URIRef, URIRef | None] = {}
    for c in classes:
        parent_in_set[c] = _closest_ancestor_in_set(ontology, c, class_set)

    children_of: dict[URIRef | None, list[URIRef]] = {}
    for c, p in parent_in_set.items():
        children_of.setdefault(p, []).append(c)

    for siblings in children_of.values():
        siblings.sort(key=str)

    lines: list[str] = []

    def _emit(cls: URIRef, depth: int) -> None:
        indent  = "  " * depth
        label   = axioms.class_label(ontology, cls)
        defn    = axioms.class_definition(ontology, cls)
        defn_short = (defn[:80] + "…") if len(defn) > 80 else defn
        suffix  = f" — {defn_short}" if defn_short else ""
        curie   = _curie(cls)
        lines.append(f"{indent}- {curie}: {label}{suffix}")
        for child in children_of.get(cls, []):
            _emit(child, depth + 1)

    for root in children_of.get(None, []):
        _emit(root, 0)

    return "\n".join(lines)


# ── LLM call ──────────────────────────────────────────────────────────────

_BITMAP_PROMPT = """\
You are deciding which classes from an upper ontology have at least one
instance in this document. For every class you mark TRUE, CITE EVIDENCE —
a short verbatim quote from the document that mentions an instance. If you
can't find a verbatim quote, do NOT include the class.

Mark a class TRUE if there is at least one entity in the document that
qualifies as an instance of that class. The entity does NOT need to be a
"named" thing — a process, an event, a quantity, or an unnamed activity all
count if the document describes them concretely.

  Examples (dental invoice):
    - Activity → TRUE, evidence "professional tooth cleaning rendered on 17.01.2025"
      (the activity is real even though it has no proper noun)
    - Person → TRUE, evidence "Dmitrii Shishkin"
    - Organization → TRUE, evidence "Zahnarztpraxis Liebermann"
    - PointInTime → TRUE, evidence "17.01.2025"
    - PhysicalObject → FALSE
      (no specific tools, equipment, or substances mentioned with concrete instances)

It's fine to mark both a class and its subclass when both have instances —
downstream type refinement narrows specifics later. Just don't mark a class
because it's *technically* a supertype of something extracted; require an
actual instance with cited evidence.

Document title: {title!r}

Document excerpt:
\"\"\"
{excerpt}
\"\"\"

Class hierarchy (children are indented under their parents; the CURIE before
each label is what you should use as the JSON key):

{hierarchy}

Reply in JSON only, no prose:

{{
  "selected": [
    {{"class": "<curie>", "evidence": "<short verbatim quote ≤80 chars>"}},
    ...
  ],
  "rationale": "one short sentence"
}}

Empty list is valid if no classes have instances in this document.
Use the exact CURIE strings from the tree above (case-sensitive).
"""


def expand_with_range_coupling(
    ontology: Graph,
    selected: list[URIRef],
    *,
    max_iterations: int = 3,
) -> tuple[list[URIRef], list[URIRef]]:
    """Expand *selected* with class ranges of its (domain-matched) properties.

    If a selected class C has a property P with range R (where R is an
    extractable class), include R too — otherwise the property can never be
    filled because no entity of type R will be extracted to point at.

    Concrete case: ScalarQuantityDatum has `lis:datumUOM → UnitOfMeasure`.
    Selecting ScalarQuantityDatum auto-pulls UnitOfMeasure into the branch
    list, so the walker extracts UoM entities even when the bitmap LLM didn't
    mark UnitOfMeasure directly. Without coupling, every quantity datum loses
    its unit-of-measure link to LLM bitmap variance.

    Only DOMAIN-MATCHED properties (rdfs:domain on C or one of its supers)
    contribute to coupling. Domain-less universal properties are skipped — they
    apply to every class, so coupling on them would over-broaden to most of
    the ontology.

    Iterates to fixed point (bounded by *max_iterations*) so transitive
    coupling holds (the range of a range can pull in further classes).

    Returns (final_list, added_list) where final_list preserves the original
    *selected* order with coupled additions appended, and added_list contains
    only the new classes (sorted) for caller logging.
    """
    selected_set: set[URIRef] = set(selected)
    added_order: list[URIRef] = []

    for _ in range(max_iterations):
        new_this_pass: set[URIRef] = set()
        for cls in list(selected_set):
            for prop in axioms.properties_of(ontology, cls, include_inherited=True):
                if not axioms.is_extractable(ontology, prop):
                    continue
                if not axioms.is_class_range(ontology, prop):
                    continue
                rng = axioms.range_of(ontology, prop)
                if rng is None or rng in selected_set:
                    continue
                if not axioms.is_extractable(ontology, rng):
                    continue
                new_this_pass.add(rng)
        if not new_this_pass:
            break
        for cls in sorted(new_this_pass, key=str):
            selected_set.add(cls)
            added_order.append(cls)

    final = list(selected) + added_order
    return final, added_order


def select_relevant_classes(
    ontology:        Graph,
    document_title:  str,
    document_excerpt: str,
    *,
    client:          LLMClient,
    model:           ModelConfig,
    namespace:       str | None = None,
    max_classes:     int = 80,
) -> BitmapResult:
    """Ask the LLM which classes have direct instances in the document.

    Returns a BitmapResult with `selected` = list of URIRefs the LLM marked
    YES. Empty list is valid (no extractable instances). On parse failure or
    too-large class set, falls back to all extractable classes (degrades
    gracefully — the walker still runs, just without the bitmap pre-filter).
    """
    classes = collect_extractable_classes(ontology, namespace=namespace)
    if not classes:
        return BitmapResult(selected=[])
    if len(classes) > max_classes:
        logger.warning(
            "bitmap: %d extractable classes exceeds max %d; falling back to all",
            len(classes), max_classes,
        )
        return BitmapResult(selected=classes)

    hierarchy = format_hierarchy(ontology, classes)
    prompt = _BITMAP_PROMPT.format(
        title     = document_title,
        excerpt   = document_excerpt,
        hierarchy = hierarchy,
    )

    meta = f"{model.model_id}  {len(classes)} classes"
    log_prompt("part14/bitmap", prompt, logger=logger, metadata=meta)
    started = datetime.now(timezone.utc)
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    ended = datetime.now(timezone.utc)
    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response("part14/bitmap", text, logger=logger, metadata=meta, as_json=True)

    parsed = _parse_bitmap_response(text, classes)
    final, coupled = expand_with_range_coupling(ontology, parsed["selected"])
    return BitmapResult(
        selected       = final,
        evidence       = parsed["evidence"],
        rationale      = parsed["rationale"],
        coupling_added = coupled,
        started        = started,
        ended          = ended,
    )


def _parse_bitmap_response(text: str, classes: list[URIRef]) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("bitmap: no JSON in response %r — falling back to all classes", text[:200])
        return {
            "selected":  classes,
            "evidence":  {},
            "rationale": "(parse error)",
        }
    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("bitmap: JSON decode failed (%s) — falling back to all", exc)
        return {
            "selected":  classes,
            "evidence":  {},
            "rationale": "(parse error)",
        }

    curie_to_uri = {_curie(c): c for c in classes}
    selected: list[URIRef]   = []
    evidence: dict[URIRef, str] = {}
    raw_selected = obj.get("selected", [])
    if not isinstance(raw_selected, list):
        raw_selected = []
    for item in raw_selected:
        # Tolerate both legacy (string CURIE) and new (object with class+evidence) formats.
        if isinstance(item, str):
            curie, ev = item, ""
        elif isinstance(item, dict):
            curie = str(item.get("class", "")).strip()
            ev    = str(item.get("evidence", "") or "").strip()
        else:
            continue
        if curie in curie_to_uri:
            uri = curie_to_uri[curie]
            selected.append(uri)
            if ev:
                evidence[uri] = ev
        elif curie:
            logger.warning("bitmap: unknown CURIE %r in selected list", curie)

    rationale = str(obj.get("rationale", "") or "")
    return {"selected": selected, "evidence": evidence, "rationale": rationale}


def _closest_ancestor_in_set(
    ontology: Graph,
    cls:      URIRef,
    targets:  set[URIRef],
) -> URIRef | None:
    """BFS upward via direct super-classes; return the first one that's in
    *targets*. None if no ancestor is in the set."""
    visited: set[URIRef] = {cls}
    frontier: set[URIRef] = {cls}
    while frontier:
        next_frontier: set[URIRef] = set()
        for c in frontier:
            for sup in axioms.superclasses(ontology, c, direct=True):
                if sup in visited:
                    continue
                if sup in targets:
                    return sup
                visited.add(sup)
                next_frontier.add(sup)
        frontier = next_frontier
    return None


# ── Helpers ───────────────────────────────────────────────────────────────

# Common namespace → CURIE prefix mapping. Could be derived from the dataset's
# binding table instead; keeping a small static map for predictable prompt output.
_PREFIX_MAP: dict[str, str] = {
    "http://rds.posccaesar.org/ontology/lis14/rdl/": "lis",
    "http://example.org/docgraph/meta#":          "dg",
    "http://www.w3.org/ns/oa#":                   "oa",
    "http://www.w3.org/ns/prov#":                 "prov",
    "http://www.w3.org/2002/07/owl#":             "owl",
    "http://www.w3.org/2000/01/rdf-schema#":      "rdfs",
    "http://www.w3.org/2001/XMLSchema#":          "xsd",
    "http://purl.org/dc/terms/":                  "dcterms",
}


def _curie(uri: URIRef) -> str:
    s = str(uri)
    for ns, prefix in _PREFIX_MAP.items():
        if s.startswith(ns):
            return f"{prefix}:{s[len(ns):]}"
    # Unknown namespace — fall back to the URI's local name with a numeric prefix
    for sep in ("#", "/"):
        if sep in s:
            return f"<{s}>"
    return s
