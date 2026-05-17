"""Shared dataclasses for the docgraph pipeline."""

from dataclasses import dataclass

from rdflib import URIRef


@dataclass
class ModelConfig:
    uri: URIRef      # canonical URI from the ontology, e.g. llm:claude-haiku-4-5
    model_id: str    # API identifier, e.g. "claude-haiku-4-5-20251001"
    label: str
    provider: str    # local name of llm:provider individual, e.g. "anthropic", "openai"
