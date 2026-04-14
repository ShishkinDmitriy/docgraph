"""Anthropic SDK implementation of LLMClient."""

import anthropic

from . import LLMClient, ModelResponse, TextBlock, ToolUseBlock

_PDF_BETA_HEADER = "pdfs-2024-09-25"


def _has_document(messages: list[dict]) -> bool:
    """Return True if any message content contains a document (PDF) block."""
    for msg in messages:
        for block in msg.get("content", []):
            if isinstance(block, dict) and block.get("type") == "document":
                return True
    return False


class AnthropicClient:
    """Wraps anthropic.Anthropic and normalizes responses to ModelResponse."""

    def __init__(self, api_key: str):
        self._client = anthropic.Anthropic(api_key=api_key)

    def create(
        self,
        *,
        model_id: str,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = [],
        max_tokens: int = 4096,
    ) -> ModelResponse:
        kwargs: dict = {}
        if _has_document(messages):
            kwargs["extra_headers"] = {"anthropic-beta": _PDF_BETA_HEADER}

        resp = self._client.messages.create(
            model=model_id,
            messages=messages,
            system=system,
            tools=tools or [],
            max_tokens=max_tokens,
            **kwargs,
        )

        content: list[TextBlock | ToolUseBlock] = []
        assistant_message: list[dict] = []

        for block in resp.content:
            if block.type == "text":
                content.append(TextBlock(text=block.text))
                assistant_message.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content.append(ToolUseBlock(
                    id=block.id, name=block.name, input=dict(block.input)
                ))
                assistant_message.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input),
                })

        return ModelResponse(
            content=content,
            stop_reason=resp.stop_reason,
            assistant_message=assistant_message,
        )
