"""LLM-driven classification of an ingested document.

Step 4a of the extraction pipeline (see ARCHITECTURE.md): given a document's
markdown content and the current set of subclasses of ``lis:InformationObject``,
ask the LLM which one fits best (or "none of the above").

Future slices will add 4b (`dg:isAbout` subjects), 5/6 (definitional detection
+ concept extraction), and 7 (instance property extraction).
"""

import logging
from dataclasses import dataclass

from rdflib import Dataset, URIRef

from src.classifier import _parse_json_response
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)

# Hard cap on how much markdown we send. ~32k chars ≈ ~8k tokens — enough for
# multi-page documents while staying well under any model context limit.
_MARKDOWN_BUDGET = 32_000


@dataclass
class TypeCandidate:
    uri: URIRef
    label: str
    comment: str


@dataclass
class TypeChoice:
    uri: URIRef | None    # None == "none of the candidates fits"
    confidence: float
    reason: str


def information_object_subclasses(ds: Dataset) -> list[TypeCandidate]:
    """Transitive subclasses of ``lis:InformationObject`` (excluding the class itself)."""
    query = """
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    PREFIX lis:  <http://standards.iso.org/iso/15926/part14/>
    SELECT DISTINCT ?cls ?label ?comment WHERE {
      ?cls rdfs:subClassOf+ lis:InformationObject .
      OPTIONAL { ?cls rdfs:label   ?label }
      OPTIONAL { ?cls rdfs:comment ?comment }
      FILTER (?cls != lis:InformationObject)
    }
    ORDER BY ?cls
    """
    seen: dict[URIRef, TypeCandidate] = {}
    for row in ds.query(query):
        uri = row.cls
        if uri in seen:
            continue
        label   = str(row.label)   if row.label   is not None else _local_name(uri)
        comment = str(row.comment) if row.comment is not None else ""
        seen[uri] = TypeCandidate(uri=uri, label=label, comment=comment)
    return list(seen.values())


def classify_document_type(
    markdown: str,
    candidates: list[TypeCandidate],
    client,
    model: ModelConfig,
) -> TypeChoice:
    """Ask the LLM to pick the best-matching class for the document."""
    if not candidates:
        return TypeChoice(uri=None, confidence=0.0,
                          reason="no candidate classes in the graph")

    none_idx = len(candidates) + 1
    cand_lines = []
    for i, c in enumerate(candidates, 1):
        line = f"{i}. {c.label}  <{c.uri}>"
        if c.comment:
            snippet = c.comment.replace("\n", " ").strip()
            if len(snippet) > 240:
                snippet = snippet[:237] + "..."
            line += f"\n   — {snippet}"
        cand_lines.append(line)

    prompt = f"""You are classifying a document into one of several candidate types.

Each candidate is a subclass of `lis:InformationObject` (any document is at minimum an
information object). Pick the BEST single match, or "{none_idx}. None" if no candidate
fits — it is better to leave the document generically typed than to force a poor match.

Document content:
---
{_truncate(markdown, _MARKDOWN_BUDGET)}
---

Candidate types:
{chr(10).join(cand_lines)}
{none_idx}. None of the above — leave the document as a generic lis:InformationObject.

Reply with a single JSON object, no prose, no fences:
{{
  "choice":     <integer 1..{none_idx}>,
  "confidence": <number 0.0..1.0>,
  "reason":     "<one-sentence explanation>"
}}
"""

    meta = f"{model.model_id}  max_tokens=512  candidates={len(candidates)}"
    log_prompt("classify_document_type", prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    log_response("classify_document_type", raw, logger=logger, metadata=meta, as_json=True)

    data = _parse_json_response(raw)
    try:
        choice = int(data.get("choice"))
    except (TypeError, ValueError):
        choice = none_idx
    confidence = float(data.get("confidence", 0.0) or 0.0)
    reason     = str(data.get("reason", "") or "")

    if choice < 1 or choice > none_idx or choice == none_idx:
        return TypeChoice(uri=None, confidence=confidence, reason=reason)
    return TypeChoice(uri=candidates[choice - 1].uri, confidence=confidence, reason=reason)


def _truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    head = budget * 3 // 4
    tail = budget - head - 20
    return f"{text[:head]}\n\n[... {len(text) - head - tail} chars truncated ...]\n\n{text[-tail:]}"


def _local_name(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s or str(uri)
