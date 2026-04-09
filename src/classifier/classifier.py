"""Classify tax documents using Claude."""

import json
import logging
import re

import anthropic

from .models import ClassificationResult, DocumentHit, Messages, ModelConfig

logger = logging.getLogger(__name__)

MARKDOWN_PROMPT = """Analyse this PDF and convert its content to Markdown.

First decide whether the PDF contains one document or several distinct documents \
(e.g. an invoice on page 1 and a payment receipt on page 2).  Split at clear \
document boundaries such as separate headers, different issuers, or page breaks \
that introduce a new document type.  Do NOT split sections of the same document.

If any page carries explicit pagination (e.g. "Page 1 of 2", "Seite 1/3", "1/2") \
treat the entire paginated sequence as one document, regardless of how many pages it spans.

Also identify any visual stamps, seals, handwritten notes, or annotations on each \
document (e.g. "PAID", "RECEIVED", "APPROVED", date stamps, rubber stamps, signatures).

Whenever text is partially hidden, obscured, illegible, cut off, or covered \
(e.g. by a stamp, fold, redaction, or poor scan quality), mark the affected \
spot inline with `[UNCLEAR: <reason>]`, e.g.:
  Tel: 012 345 [UNCLEAR: last 4 digits hidden by stamp]
  IBAN: DE89 3704 [UNCLEAR: remainder cut off at page edge]

Respond with JSON only, no prose:
{
  "documents": [
    {
      "title": "<short human-readable title, e.g. Invoice, Receipt, Bank Statement>",
      "description": "<2-4 sentence summary covering: document type, issuer, recipient, key dates, amounts, and purpose — anything useful for downstream classification or data extraction>",
      "markdown": "<full content of this document as Markdown, with [UNCLEAR: reason] markers where extraction failed>",
      "stamps": ["<stamp or annotation text>", ...],
      "issues": ["<one-line description of each extraction problem, e.g. 'Phone number partially hidden by PAID stamp'>", ...]
    }
  ]
}

Rules:
- Always return at least one entry in "documents".
- Use an empty list for "stamps" and "issues" when none are found.
- Preserve all text, tables, and numeric values faithfully in "markdown"."""

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

    Returns a list of dicts, each with keys "title", "markdown", "stamps".
    A single-document PDF returns a one-element list.
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
    # Normalise: ensure stamps key exists on every entry
    for doc in docs:
        doc.setdefault("stamps", [])
        doc.setdefault("title", "Document")
        doc.setdefault("description", "")
        doc.setdefault("issues", [])
    return docs


def markdown_content_block(docs: list[ExtractedDoc]) -> dict:
    """
    Build a prompt-cached text content block from one or more extracted documents.
    Pass this block to `classify_pdf()` instead of the raw PDF block so that
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


def classify_pdf(
    content_block: dict,
    client: anthropic.Anthropic,
    categories: dict[str, str],
    model: ModelConfig,
    note: str | None = None,
) -> tuple[ClassificationResult, Messages]:
    """
    Classify a document via the Anthropic API.

    `content_block` can be either:
    - a raw PDF document block (from `extract_pdf`)
    - a Markdown text block (from `markdown_content_block`) — preferred, cheaper
      for follow-up calls because the cached text is reused at ~10 % token cost.

    Returns (result, messages) so callers can issue follow-up extraction calls
    without re-sending the document.
    """
    system = SYSTEM_PROMPT.format(categories=_format_categories(categories))
    prompt = "Classify this document:\n\nRespond with JSON only, no prose."
    if note:
        prompt += f"\n\nNote from user: {note}"
    user_content = [content_block, {"type": "text", "text": prompt}]

    messages: Messages = [{"role": "user", "content": user_content}]

    logger.debug("classify_pdf | system prompt:\n%s", system)
    logger.debug("classify_pdf | prompt:\n%s", prompt)

    response = client.messages.create(
        model=model.model_id,
        max_tokens=512,
        system=system,
        messages=messages,
        extra_headers={"anthropic-beta": "pdfs-2024-09-25"},
    )

    raw = response.content[0].text
    logger.debug("classify_pdf | response:\n%s", raw)
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
