"""Classify tax documents using Claude."""

import json
import logging
import re

import anthropic

from .models import ClassificationResult, DocumentHit, Messages, ModelConfig

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a financial document classifier. Given extracted image from a PDF, identify every financial document type present in it.

IMPORTANT: a single PDF may contain more than one document type simultaneously. \
For example, a document that demands payment (Bill) may also include a section \
confirming a prior payment (Receipt). Return ALL detected types.

Available document types:

{categories}

Respond with JSON only, no prose:
{{
  "documents": [
    {{
      "category": "<category key from the list above>",
      "confidence": <0.0-1.0>,
      "reason": "<one sentence>"
    }}
  ]
}}

Rules:
- List documents in descending order of confidence.
- Omit any type whose confidence is below 0.1.
- Use only the exact category keys defined above."""


def _strip_fences(raw: str) -> str:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    return re.sub(r"\s*```$", "", text).strip()


def _parse_json_response(raw: str) -> dict:
    """Parse Claude's response, tolerating markdown code fences."""
    return json.loads(_strip_fences(raw))


def _parse_year(value: object) -> int | None:
    """Convert the LLM's year field to an int, returning None on failure."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _format_categories(categories: dict[str, str]) -> str:
    """Format {notation: description} into a numbered list for the system prompt."""
    parts = []
    for key, desc in categories.items():
        # desc may be a multi-line rdfs:comment; normalise whitespace between sentences
        body = " ".join(desc.split())
        parts.append(f"**{key}**: {body}")
    return "\n\n".join(parts)


def _parse_classification(raw: str) -> ClassificationResult:
    """Parse the LLM's multi-doc JSON response into a ClassificationResult."""
    data = _parse_json_response(raw)
    hits = [
        DocumentHit(
            category=d["category"],
            confidence=float(d["confidence"]),
            reason=d["reason"],
        )
        for d in data.get("documents", [])
    ]
    # Ensure sorted by confidence desc
    hits.sort(key=lambda h: h.confidence, reverse=True)
    return ClassificationResult(documents=hits)


def classify(
    text: str,
    client: anthropic.Anthropic,
    categories: dict[str, str],
    model: ModelConfig,
) -> tuple[ClassificationResult, Messages]:
    """
    Classify a text-based PDF.
    Returns (result, messages) for consistency with classify_from_images.
    """
    system = SYSTEM_PROMPT.format(categories=_format_categories(categories))
    user_text = f"Classify this document:\n\n{text[:4000]}"
    messages: Messages = [{"role": "user", "content": user_text}]

    logger.debug("classify | system prompt:\n%s", system)
    logger.debug("classify | user message:\n%s", user_text)

    response = client.messages.create(
        model=model.model_id,
        max_tokens=512,
        system=system,
        messages=messages,
    )

    raw = response.content[0].text
    # logger.debug("classify | response:\n%s", raw)
    messages.append({"role": "assistant", "content": raw})

    return _parse_classification(raw), messages


def classify_from_images(
    images: list[dict],
    client: anthropic.Anthropic,
    categories: dict[str, str],
    model: ModelConfig,
) -> tuple[ClassificationResult, Messages]:
    """
    Classify a scanned PDF via Claude vision.
    Returns (result, messages) — messages is the full conversation history
    with the images already in place, ready for follow-up extraction.
    """
    system = SYSTEM_PROMPT.format(categories=_format_categories(categories))
    # Strip internal _path metadata before sending to the API
    api_images = [{k: v for k, v in img.items() if k != "_path"} for img in images]
    user_content = api_images + [
        {"type": "text", "text": "Classify this document:\n\nRespond with JSON only, no prose."}
    ]

    messages: Messages = [{"role": "user", "content": user_content}]

    text_parts = "\n".join(
        item["text"] for item in user_content if item.get("type") == "text"
    )
    logger.debug("classify_from_images | system prompt:\n%s", system)
    logger.debug("classify_from_images | user text:\n%s", text_parts)

    response = client.messages.create(
        model=model.model_id,
        max_tokens=512,
        system=system,
        messages=messages,
    )

    raw = response.content[0].text
    logger.debug("classify_from_images | response:\n%s", raw)
    messages.append({"role": "assistant", "content": raw})

    return _parse_classification(raw), messages


def extract_details(
    messages: Messages,
    prompt: str,
    client: anthropic.Anthropic,
    model: ModelConfig,
) -> dict:
    """
    Ask a follow-up extraction question reusing the existing conversation.
    The images (or text) from the first turn are already in `messages`.
    Returns parsed JSON from the response.
    """
    follow_up = messages + [{"role": "user", "content": prompt}]

    logger.debug("extract_details | follow-up prompt:\n%s", prompt)

    response = client.messages.create(
        model=model.model_id,
        max_tokens=512,
        messages=follow_up,
    )

    raw = response.content[0].text
    logger.debug("extract_details | response:\n%s", raw)
    return _parse_json_response(raw)
