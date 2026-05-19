"""setup_llm — populate ctx["client"] and ctx["model"] for LLM-using tasks.

Foundational task that the per-doc pipeline (identity → convert →
extract → templates) depends on. Always populates ctx["model"]
(hardcoded for now). Populates ctx["client"] only if
`ANTHROPIC_API_KEY` is set; otherwise leaves it None — read-only
flows (snapshot, diagram, history, …) don't need the client and
shouldn't fail just because the env var is missing. LLM-using task
bodies should `require_client(ctx)` before calling it.

No dirty check — idempotent, runs at most once per `run()` call.
Skips if ctx already has "client" (test setups pre-populate).

ctx contract: nothing required from the caller.
"""

from __future__ import annotations

import os

from rdflib import URIRef

from src.llm.anthropic import AnthropicClient
from src.models import ModelConfig
from src.sources import IngestError
from src.tasks._registry import docgraph

# Hardcoded vision model for PDF→Markdown conversion. Make this configurable
# (config.ttl in the project) once we have more than one option.
_DEFAULT_MODEL = ModelConfig(
    uri      = URIRef("http://example.org/docgraph/agent/claude-haiku-4-5"),
    model_id = "claude-haiku-4-5",
    label    = "Claude Haiku 4.5",
    provider = "anthropic",
)


@docgraph.task
def setup_llm(ctx) -> None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    ctx["client"] = AnthropicClient(api_key=api_key) if api_key else None
    ctx["model"]  = _DEFAULT_MODEL


@docgraph.dirty
def setup_llm_dirty(ctx) -> bool:
    return "client" not in ctx


def require_client(ctx) -> AnthropicClient:
    """Fetch the LLM client from ctx, raising a clear error if it's not
    configured. Use this in task bodies right before any actual LLM call."""
    client = ctx.get("client")
    if client is None:
        raise IngestError("ANTHROPIC_API_KEY environment variable not set")
    return client
