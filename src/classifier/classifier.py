"""PDF to Markdown extraction using Claude."""

import json
import logging
import re

import anthropic

from .models import ModelConfig
from .prompts import MARKDOWN_PROMPT

logger = logging.getLogger(__name__)


def _strip_fences(raw: str) -> str:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    return re.sub(r"\s*```$", "", text).strip()


_VALID_JSON_ESCAPES = set(r'"\\/bfnrtu')


def _fix_invalid_escapes(s: str) -> str:
    """Replace invalid JSON escape sequences (e.g. \_ \* \[ \]) with the literal character."""
    result = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s) and s[i + 1] not in _VALID_JSON_ESCAPES:
            # Drop the backslash, keep the next character as-is
            result.append(s[i + 1])
            i += 2
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


def _parse_json_response(raw: str) -> dict:
    """Parse Claude's response, tolerating markdown code fences and invalid escape sequences."""
    text = _strip_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(_fix_invalid_escapes(text))


ExtractedDoc = dict  # {"title": str, "markdown": str, "stamps": list[str]}


def pdf_to_markdown(
    pdf_block: dict,
    client: anthropic.Anthropic,
    model: ModelConfig,
    note: str | None = None,
) -> list[ExtractedDoc]:
    """
    First-pass extraction: send the PDF to Claude and get back one or more
    Markdown documents with their stamps/annotations.

    Returns a list of dicts, each with keys "title", "description", "markdown",
    "stamps", "issues".
    """
    prompt = MARKDOWN_PROMPT
    if note:
        prompt += f"\n\nNote from user: {note}"
    response = client.messages.create(
        model=model.model_id,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [pdf_block, {"type": "text", "text": prompt}],
        }],
        extra_headers={"anthropic-beta": "pdfs-2024-09-25"},
    )
    raw = response.content[0].text
    logger.debug("pdf_to_markdown | response:\n%s", raw)
    data = _parse_json_response(raw)
    docs = data.get("documents", [])
    for doc in docs:
        doc.setdefault("stamps", [])
        doc.setdefault("title", "Document")
        doc.setdefault("description", "")
        doc.setdefault("issues", [])
    return docs


def markdown_content_block(docs: list[ExtractedDoc]) -> dict:
    """
    Build a prompt-cached text content block from one or more extracted documents.
    Pass this to the classifier/extractor instead of the raw PDF block so that
    follow-up calls reuse the cached text at ~10 % token cost.
    """
    parts = []
    for i, doc in enumerate(docs, 1):
        header = f"## Document {i}: {doc['title']}" if len(docs) > 1 else f"## {doc['title']}"
        section = header
        if doc["description"]:
            section += f"\n\n> {doc['description']}"
        section += f"\n\n{doc['markdown']}"
        if doc["stamps"]:
            section += f"\n\n*Stamps / annotations: {', '.join(doc['stamps'])}*"
        parts.append(section)

    text = "\n\n---\n\n".join(parts)
    return {
        "type": "text",
        "text": text,
        "cache_control": {"type": "ephemeral"},
    }
