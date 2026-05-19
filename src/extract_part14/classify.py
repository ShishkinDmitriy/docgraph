"""Subject classification for the part14 pipeline.

Asks the LLM "what is this document about?" and picks one or more answers
from the candidate set derived from the loaded upper ontology — not a
hardcoded list. For LIS-14 specifically, candidates are:
  - lis:Activity, lis:Aspect (top-level)
  - immediate children of lis:Object (FunctionalObject, InformationObject,
    Location, Organization, PhysicalObject)
  - lis:Object itself is excluded — too generic to be a useful subject

If the upper ontology changes (new release adds a class, user loads a domain
ontology that extends the top level), candidates auto-extend. No code change.

Form classification (what *kind of document* is this) is a separate step
that needs at least one user-ingested form ontology to be available;
deferred until that's wired up.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from rdflib import Graph, Namespace, URIRef

from src.extract_part14 import axioms
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")
logger = logging.getLogger(__name__)


# Per-class hints for cases where rdfs:comment / skos:definition is missing
# or too terse in the loaded ontology. LIS-14 in particular ships almost no
# class-level definitions, so we need fallback descriptions to give the LLM
# enough context to classify accurately. These migrate to dg:promptHint
# annotations in dg-part14-alignments.ttl over time.
_FALLBACK_DEFINITIONS: dict[str, str] = {
    str(LIS.Activity):         "Something that happens or is done over time — a process, event, transaction, procedure.",
    str(LIS.Aspect):           "A characteristic, property, role, or capability that something has — qualities, dispositions, functions, roles.",
    str(LIS.Object):           "Generic — a thing that exists, including objects, locations, information objects, organizations.",
    str(LIS.PhysicalObject):   "A material thing that takes up space — equipment, components, substances, biological entities.",
    str(LIS.FunctionalObject): "A thing defined by its function or role rather than its physical realisation — systems, instruments, intended-purpose entities.",
    str(LIS.InformationObject):"An abstract carrier of meaning — documents, datasets, specifications, codes, identifiers, signs.",
    str(LIS.Organization):     "A formal group of people — companies, agencies, departments, partnerships.",
    str(LIS.Location):         "A place or spatial region — sites, addresses, geographic regions, points in space.",
}


@dataclass
class SubjectCandidate:
    uri:   URIRef
    label: str
    description: str


@dataclass
class SubjectResult:
    subjects:   list[URIRef]
    confidence: float
    rationale:  str
    started:    datetime
    ended:      datetime


def subject_candidates(graph: Graph) -> list[SubjectCandidate]:
    """Compute subject classification candidates from the loaded upper
    ontology.

    For LIS-14: top-level classes (Activity, Aspect, Object) plus immediate
    children of Object (so Object's 5 substantive subdivisions show up
    directly as candidates), minus Object itself (too generic).

    For other upper ontologies: top-level classes only. The "descend one
    level into the broadest top-level class" trick is LIS-14 specific
    because Object is unusually broad; doing it always would over-include.
    """
    LIS_NS = str(LIS)
    tops = axioms.top_level_classes(graph, namespace=LIS_NS)

    candidates: list[URIRef] = []
    object_uri = URIRef(LIS_NS + "Object")
    for cls in tops:
        if cls == object_uri:
            # Drop Object itself; descend into its immediate children
            for child in axioms.subclasses(graph, object_uri, direct=True):
                if str(child).startswith(LIS_NS):
                    candidates.append(child)
        else:
            candidates.append(cls)

    out: list[SubjectCandidate] = []
    for uri in candidates:
        if not axioms.is_extractable(graph, uri):
            continue
        label = axioms.class_label(graph, uri)
        defn  = axioms.class_definition(graph, uri) or _FALLBACK_DEFINITIONS.get(str(uri), "")
        out.append(SubjectCandidate(uri=uri, label=label, description=defn))
    return out


def classify_subject(
    document_title: str,
    document_excerpt: str,
    *,
    candidates: list[SubjectCandidate],
    client: LLMClient,
    model: ModelConfig,
) -> SubjectResult:
    """Run subject classification on a document.

    *document_excerpt* should be a few hundred words — typically the first
    page or so of the markdown.
    *candidates* is the candidate set computed from the loaded ontology
    (see `subject_candidates()`); injected so the caller decides what's in
    scope (allowing future user-loaded ontologies to extend the set).
    """
    prompt = _build_prompt(document_title, document_excerpt, candidates)
    meta = f"{model.model_id}  {len(candidates)} candidates"
    log_prompt("part14/subject", prompt, logger=logger, metadata=meta)
    started = _now()
    response = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=512,
    )
    ended = _now()

    text = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
    log_response("part14/subject", text, logger=logger, metadata=meta, as_json=True)
    parsed = _parse_response(text, candidates)
    return SubjectResult(
        subjects=parsed["subjects"],
        confidence=parsed["confidence"],
        rationale=parsed["rationale"],
        started=started,
        ended=ended,
    )


def _build_prompt(title: str, excerpt: str, candidates: list[SubjectCandidate]) -> str:
    candidates_block = "\n".join(
        f"- {c.label}: {c.description}" for c in candidates
    )
    return f"""\
You are classifying what a document is *about* against an upper ontology.
A document can be about more than one thing; pick all that clearly apply
(typically 1–3).

Candidates:

{candidates_block}

Document title: {title!r}

Document excerpt:
\"\"\"
{excerpt}
\"\"\"

Reply in JSON only, no prose, with this exact shape:

{{
  "subjects": ["<label-1>", "<label-2>"],
  "confidence": 0.85,
  "rationale": "one sentence explaining the choice"
}}

Use the exact label strings from the candidates list above (case-sensitive).
Confidence is your headline confidence in the whole answer (0.0 to 1.0).
"""


def _parse_response(text: str, candidates: list[SubjectCandidate]) -> dict:
    """Extract the JSON object from the LLM response. Tolerant of code-fence
    wrapping and leading/trailing prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        logger.warning("subject classification: no JSON object in response %r", text)
        return {"subjects": [], "confidence": 0.0, "rationale": "(parse error)"}
    try:
        obj = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        logger.warning("subject classification: JSON decode failed (%s) on %r", exc, cleaned)
        return {"subjects": [], "confidence": 0.0, "rationale": "(parse error)"}

    label_to_uri = {c.label: c.uri for c in candidates}
    subjects: list[URIRef] = []
    for label in obj.get("subjects", []):
        if label in label_to_uri:
            subjects.append(label_to_uri[label])
        else:
            logger.warning("subject classification: unknown candidate label %r", label)

    return {
        "subjects":   subjects,
        "confidence": float(obj.get("confidence", 0.0) or 0.0),
        "rationale":  str(obj.get("rationale", "") or ""),
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)
