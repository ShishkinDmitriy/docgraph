"""OpenAI-compatible implementation of LLMClient.

The agent loop builds messages in Anthropic format (tool results embedded in
user messages, tool definitions with ``input_schema``).  This client
translates to OpenAI format before every request and converts the response
back to Anthropic format so the loop never needs to change.
"""

import json

from openai import OpenAI

from . import LLMClient, ModelResponse, TextBlock, ToolUseBlock

_STOP_REASON = {
    "stop":       "end_turn",
    "tool_calls": "tool_use",
    "length":     "max_tokens",
}


def _to_openai_tools(tools: list[dict]) -> list[dict]:
    """``input_schema`` → ``parameters``, wrapped in OpenAI's function envelope."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            },
        }
        for t in tools
    ]


def _to_openai_messages(messages: list[dict], system: str) -> list[dict]:
    """
    Convert Anthropic-format messages to OpenAI format.

    Key differences handled:
    - System prompt → first ``{"role": "system", ...}`` message
    - Tool results embedded in a user message → individual ``role: "tool"`` messages
    - Assistant content list → ``tool_calls`` array + optional text ``content``
    - PDF document blocks → NotImplementedError (not supported by OpenAI)
    """
    result: list[dict] = []

    if system:
        result.append({"role": "system", "content": system})

    for msg in messages:
        role    = msg["role"]
        content = msg.get("content", [])

        if role == "user":
            if isinstance(content, list) and content and all(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            ):
                # Tool-result turn → one message per result
                for block in content:
                    result.append({
                        "role":         "tool",
                        "tool_call_id": block["tool_use_id"],
                        "content":      block.get("content", ""),
                    })
            else:
                # Regular user turn — flatten text blocks; reject PDF blocks
                parts: list[str] = []
                for block in (content if isinstance(content, list) else [content]):
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        parts.append(block["text"])
                    elif btype == "document":
                        raise NotImplementedError(
                            "OpenAI backend does not support native PDF blocks. "
                            "Run PDF extraction with AnthropicClient first so the "
                            "Markdown cache is populated, then switch backends."
                        )
                result.append({"role": "user", "content": "\n\n".join(parts)})

        elif role == "assistant":
            text       = ""
            tool_calls = []
            for block in (content if isinstance(content, list) else []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text = block["text"]
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id":   block["id"],
                        "type": "function",
                        "function": {
                            "name":      block["name"],
                            "arguments": json.dumps(block["input"], ensure_ascii=False),
                        },
                    })
            oai: dict = {"role": "assistant", "content": text or None}
            if tool_calls:
                oai["tool_calls"] = tool_calls
            result.append(oai)

    return result


class OpenAIClient:
    """Wraps ``openai.OpenAI`` and normalizes responses to ModelResponse.

    Messages are expected in Anthropic format (what the agent loop produces).
    They are translated to OpenAI format before sending and the response is
    translated back so ``assistant_message`` stays in Anthropic format.
    """

    def __init__(self, api_key: str, base_url: str | None = None):
        self._client = OpenAI(api_key=api_key, **({"base_url": base_url} if base_url else {}))

    def create(
        self,
        *,
        model_id: str,
        messages: list[dict],
        system: str = "",
        tools: list[dict] = [],
        max_tokens: int = 4096,
    ) -> ModelResponse:
        oai_messages = _to_openai_messages(messages, system)
        kwargs: dict = {}
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        resp = self._client.chat.completions.create(
            model=model_id,
            messages=oai_messages,
            max_tokens=max_tokens,
            **kwargs,
        )

        choice = resp.choices[0]
        msg    = choice.message

        content:           list[TextBlock | ToolUseBlock] = []
        assistant_message: list[dict]                     = []

        if msg.content:
            content.append(TextBlock(text=msg.content))
            assistant_message.append({"type": "text", "text": msg.content})

        for tc in msg.tool_calls or []:
            inp = json.loads(tc.function.arguments)
            content.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=inp))
            assistant_message.append({
                "type":  "tool_use",
                "id":    tc.id,
                "name":  tc.function.name,
                "input": inp,
            })

        return ModelResponse(
            content=content,
            stop_reason=_STOP_REASON.get(choice.finish_reason, "end_turn"),
            assistant_message=assistant_message,
        )
