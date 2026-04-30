"""Shared types passed between the pipeline and per-prompt converters."""

from dataclasses import dataclass, field

from rdflib import URIRef


@dataclass
class EntityRef:
    """A pointer to one extracted entity, recorded for cross-prompt reference."""
    id: str       # the slug used by the LLM in its JSON output
    kind: str     # "activity" | "individual" | "class_of_activity" |
                  # "class_of_individual" | "role" | …
    uri: URIRef   # the minted URI in this document's ext namespace
    label: str    # human-readable, for context tables passed to later prompts
    summary: str = ""    # filled by activities (used in #4's context table)
    subkind: str = ""    # filled by individuals/classes-of-individual ("person", …)


@dataclass
class ConversionContext:
    """Per-document state threaded through the pipeline.

    Each converter reads the registry to resolve cross-prompt references,
    and writes new EntityRefs for its own outputs.
    """
    source_uri: URIRef
    source_slug: str
    ext_ns: object              # Namespace for this source (mint_ext target)
    doc_kind: str = ""
    primary_subjects: list[str] = field(default_factory=list)
    entities: dict[str, EntityRef] = field(default_factory=dict)
    # Memo dictionaries shared across converters (so we don't double-emit
    # ad-hoc ClassOf<X> subclasses or Scale URIs):
    classes_minted: dict[str, URIRef] = field(default_factory=dict)
    scales_minted: dict[str, URIRef] = field(default_factory=dict)

    def register(self, ref: EntityRef) -> None:
        self.entities[ref.id] = ref

    def by_kind(self, *kinds: str) -> list[EntityRef]:
        return [r for r in self.entities.values() if r.kind in kinds]

    def get(self, id: str) -> EntityRef | None:
        return self.entities.get(id)
