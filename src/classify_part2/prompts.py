"""Loader for prompt bodies stored in ``docs/classify_prompts/NN_*.md``.

The markdown files are the source of truth for prompt content. Each file
contains a single fenced ``` block holding the prompt body (with
``{placeholders}`` for runtime substitution). This module exposes the
fenced body as a string keyed by short name.
"""

import re
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent.parent.parent / "docs" / "classify_prompts"

_FILES = {
    "nature_scan":           "01_nature_scan.md",
    "activities_events":     "02_activities_events.md",
    "individuals":           "03_individuals.md",
    "classes_of_activity":   "04_classes_of_activity.md",
    "classes_of_individual": "05_classes_of_individual.md",
    "roles":                 "06_roles.md",
    "participations":        "07_participations.md",
    "whole_parts":           "08_whole_parts.md",
    "temporal_relations":    "09_temporal_relations.md",
    "properties":            "10_properties.md",
    "quantities":            "11_quantities.md",
    "identifiers":           "12_identifiers_descriptions.md",
    "connections":           "13_connections.md",
    "lifecycle_approvals":   "14_lifecycle_approvals.md",
}

_FENCED = re.compile(r"^```\s*\n(.*?)^```", re.DOTALL | re.MULTILINE)


def available() -> list[str]:
    return list(_FILES)


def load(name: str) -> str:
    """Return the first fenced code block of the named prompt file."""
    if name not in _FILES:
        raise KeyError(f"unknown prompt {name!r}; valid: {sorted(_FILES)}")
    path = _PROMPTS_DIR / _FILES[name]
    text = path.read_text(encoding="utf-8")
    m = _FENCED.search(text)
    if not m:
        raise ValueError(f"no fenced code block found in {path}")
    return m.group(1)
