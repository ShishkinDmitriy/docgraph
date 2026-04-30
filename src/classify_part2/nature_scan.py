"""Prompt #1 — document nature scan.

Runs the LLM, parses the JSON answer set, computes the two coverage
metrics, and produces a gating decision (which of prompts 2-14 to run).
"""

import logging
from dataclasses import dataclass, field

from src.classifier import _parse_json_response
from src.classify_part2.prompts import load
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)

# Maps each yes/no question key to the set of follow-up prompt short names
# it triggers. Mirrors the table in docs/classify_design.md.
GATING_RULES: dict[str, set[str]] = {
    "describes_activities":      {"activities_events", "participations"},
    "describes_individuals":     {"individuals"},
    "defines_classes":           {"classes_of_activity", "classes_of_individual"},
    "describes_roles":           {"roles", "participations"},
    "has_temporal_structure":    {"temporal_relations"},
    "describes_whole_parts":     {"whole_parts"},
    "has_properties":            {"properties"},
    "has_quantities":            {"quantities"},
    "has_identifiers":           {"identifiers"},
    "describes_connections":     {"connections"},
    "has_lifecycle_or_approval": {"lifecycle_approvals"},
}

QUESTIONS = list(GATING_RULES)  # canonical order


@dataclass
class Answer:
    yes: bool
    evidence: str = ""


@dataclass
class NatureScanResult:
    doc_kind: str
    primary_subjects: list[str]
    answers: dict[str, Answer]
    evidence_coverage: float = 0.0
    scope_coverage:    float = 0.0
    raw: dict | None = field(default=None, repr=False)


def run(markdown: str, client, model: ModelConfig) -> NatureScanResult:
    """Run prompt #1 against *markdown* and return the parsed result."""
    template = load("nature_scan")
    prompt = template.replace("{markdown}", markdown)

    meta = f"{model.model_id}  max_tokens=1024"
    log_prompt("classify/nature_scan", prompt, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text
    log_response("classify/nature_scan", raw, logger=logger, metadata=meta, as_json=True)

    return _parse(raw, markdown)


def _parse(raw_response: str, markdown: str) -> NatureScanResult:
    data = _parse_json_response(raw_response)

    answers: dict[str, Answer] = {}
    for q in QUESTIONS:
        a = data.get("answers", {}).get(q, {}) or {}
        answers[q] = Answer(
            yes=bool(a.get("yes", False)),
            evidence=str(a.get("evidence") or ""),
        )

    primary = data.get("primary_subjects") or []
    if not isinstance(primary, list):
        primary = []

    res = NatureScanResult(
        doc_kind=str(data.get("doc_kind") or ""),
        primary_subjects=[str(s) for s in primary],
        answers=answers,
        raw=data,
    )
    _attach_metrics(res, markdown)
    return res


def _attach_metrics(res: NatureScanResult, markdown: str) -> None:
    yes_count = sum(1 for a in res.answers.values() if a.yes)
    total     = len(res.answers) or 1
    res.scope_coverage = yes_count / total

    md_chars = max(len(markdown), 1)
    quoted   = sum(len(a.evidence) for a in res.answers.values() if a.yes)
    # Cap at 1.0 — duplicated evidence quotes can over-count.
    res.evidence_coverage = min(quoted / md_chars, 1.0)


def gating_decisions(res: NatureScanResult) -> set[str]:
    """Return the set of follow-up prompt short names triggered by *res*.

    Prompt #12 ("identifiers") has a broader trigger: the design doc says
    skip only if no identifiers AND no class-defs AND no individuals.
    Encoded here so the pipeline doesn't need special-case logic.
    """
    out: set[str] = set()
    for q, prompts in GATING_RULES.items():
        if res.answers.get(q) and res.answers[q].yes:
            out.update(prompts)

    # Prompt #12 broader rule.
    a = res.answers
    if (a.get("has_identifiers")     and a["has_identifiers"].yes) \
       or (a.get("defines_classes")  and a["defines_classes"].yes) \
       or (a.get("describes_individuals") and a["describes_individuals"].yes):
        out.add("identifiers")

    # Prompt #7 needs activities AND (individuals OR roles).
    has_act   = a.get("describes_activities")  and a["describes_activities"].yes
    has_ind   = a.get("describes_individuals") and a["describes_individuals"].yes
    has_roles = a.get("describes_roles")       and a["describes_roles"].yes
    if not (has_act and (has_ind or has_roles)):
        out.discard("participations")

    return out
