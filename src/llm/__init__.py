"""Backend-agnostic LLM client protocol and normalized response types."""

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


ContentBlock = TextBlock | ToolUseBlock


@dataclass
class ModelResponse:
    """Normalized response returned by every LLMClient implementation."""
    content: list[ContentBlock]    # parsed blocks — used by agent logic
    stop_reason: str               # "end_turn" | "tool_use" | "max_tokens"
    assistant_message: list[dict]  # dict form of content, ready to append to messages


class LLMClient(Protocol):
    def create(
        self,
        *,
        model_id: str,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = [],
        max_tokens: int = 4096,
    ) -> ModelResponse: ...
