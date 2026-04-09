"""Shared dataclasses and type aliases for the tax classifier."""

from dataclasses import dataclass, field

from rdflib import URIRef
from rdflib.namespace import XSD


@dataclass
class PropertyDef:
    uri: URIRef
    field_key: str        # JSON key the LLM returns, e.g. "patient_owes"
    label: str            # human-readable, used in the extraction prompt
    rdf_range: URIRef = field(default_factory=lambda: XSD.string)
    is_object_property: bool = False   # True for flat string object ranges (fin:Service, fin:Product)
    is_monetary: bool = False          # True for tax:MonetaryAmount ranges
    comment: str = ""                  # first sentence of rdfs:comment, used as extraction hint
    is_compound_list: bool = False     # True when range is a list of compound objects (e.g. fin:LineItem)
    is_compound_object: bool = False   # True when range is a single compound object (e.g. foaf:Agent)
    item_schema: "list[PropertyDef]" = field(default_factory=list)  # sub-properties for compound types


@dataclass
class DocumentClass:
    uri: URIRef
    notation: str    # classifier key, e.g. "bill"
    definition: str  # skos:definition — short one-liner
    description: str = ""  # rdfs:comment — full description used in the LLM prompt


@dataclass
class ModelConfig:
    uri: URIRef   # canonical URI from the ontology, e.g. tax:claude-haiku-4-5
    model_id: str # API identifier, e.g. "claude-haiku-4-5-20251001"
    label: str


@dataclass
class DocumentHit:
    """One detected document type within a (possibly composite) PDF."""
    category: str
    confidence: float
    reason: str
    details: dict | None = None


@dataclass
class ClassificationResult:
    documents: list[DocumentHit]  # all detected types, sorted by confidence desc

    # ── Convenience accessors for the primary (highest-confidence) hit ─────────
    @property
    def category(self) -> str:
        return self.documents[0].category if self.documents else "unknown"

    @property
    def confidence(self) -> float:
        return self.documents[0].confidence if self.documents else 0.0

    @property
    def reason(self) -> str:
        return self.documents[0].reason if self.documents else ""

    @property
    def details(self) -> dict | None:
        return self.documents[0].details if self.documents else None

    @details.setter
    def details(self, value: dict | None) -> None:
        if self.documents:
            self.documents[0].details = value


# Conversation history: list of {"role": ..., "content": ...}
Messages = list[dict]
