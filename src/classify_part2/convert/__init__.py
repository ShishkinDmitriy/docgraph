"""Per-prompt JSON-to-Turtle converters.

Each module exposes ``convert(data, ctx) -> rdflib.Graph`` matching one
prompt in ``docs/classify_prompts/``. The pipeline imports the converters
by short name (matching ``src.classify_part2.prompts._FILES``).

`classes` and `properties` each cover two prompts and expose two
specifically-named entry points instead of a single ``convert``.
"""

from src.classify_part2.convert import (
    activities,
    classes,
    connections,
    identifiers,
    individuals,
    lifecycle,
    participations,
    properties,
    roles,
    temporal,
    whole_parts,
)

__all__ = [
    "activities",
    "classes",
    "connections",
    "identifiers",
    "individuals",
    "lifecycle",
    "participations",
    "properties",
    "roles",
    "temporal",
    "whole_parts",
]
