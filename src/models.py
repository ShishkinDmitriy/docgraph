"""Shared dataclasses for the tax classifier."""

from dataclasses import dataclass, field

from rdflib import URIRef


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
    class_uri: str        # full OWL class URI, e.g. http://example.org/financial/DemandForPayment
    confidence: float
    reason: str
    details: dict | None = None  # JSON-LD dict returned by the LLM


@dataclass
class ClassificationResult:
    documents: list[DocumentHit]  # all detected types, sorted by confidence desc

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
