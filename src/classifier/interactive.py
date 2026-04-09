"""Interactive dialog to fill in fields that SHACL validation found missing."""

import click

from .classifier import extract_details
from .models import DocumentHit, Messages, ModelConfig, PropertyDef
from .ontology import JSONLD_CONTEXT, prefixed_name
from .validator import ShapeViolation


def fill_missing(
    violations: list[ShapeViolation],
    hit: DocumentHit,
    class_props: list[PropertyDef],
    messages: Messages,
    client,           # anthropic.Anthropic — avoid import at module level
    model: ModelConfig,
) -> DocumentHit:
    """
    For each sh:minCount violation:
      1. Try LLM re-extraction using the existing conversation (images are
         already in `messages`, so no re-upload needed).
      2. Fall back to a click.prompt() if the LLM still returns null.

    Returns the hit with details filled in.
    The caller is responsible for re-running append_result and re-validating.
    """
    missing = [v for v in violations if v.is_missing_field and v.result_path]
    if not missing:
        return hit

    details: dict = dict(hit.details or {})
    prop_by_uri = {str(p.uri): p for p in class_props}

    for violation in missing:
        prop = prop_by_uri.get(violation.result_path)
        if prop is None:
            click.echo(f"  [missing] {violation.message}")
            continue

        prop_qname = prefixed_name(prop.uri)

        # ── 1. Ask the LLM first ──────────────────────────────────────────────
        llm_value = _ask_llm(prop_qname, prop.label, messages, client, model)

        if llm_value is not None:
            click.echo(f"  [llm] {prop.label}: {llm_value}")
            details[prop_qname] = llm_value
            continue

        # ── 2. Fall back to user prompt ───────────────────────────────────────
        click.echo(f"  [missing] {prop.label} — could not be extracted automatically")
        user_value = click.prompt(
            f"  Enter {prop.label}",
            default="",
            show_default=False,
        ).strip()

        if user_value:
            details[prop_qname] = user_value

    hit.details = details
    return hit


def _ask_llm(
    prop_qname: str,
    prop_label: str,
    messages: Messages,
    client,
    model: ModelConfig,
) -> object | None:
    """
    Send a focused follow-up question for a single missing field.
    Asks for a minimal JSON-LD fragment and returns the value, or None.
    """
    import json
    prompt = (
        f'The field "{prop_label}" ({prop_qname}) was not found earlier. '
        f'Look at the document again and return a minimal JSON-LD fragment:\n'
        f'{{"@context": {json.dumps(JSONLD_CONTEXT)}, '
        f'"{prop_qname}": <value or null>}}'
    )
    try:
        extracted = extract_details(messages, prompt, client, model)
        value = extracted.get(prop_qname)
        return None if value is None or str(value).lower() == "null" else value
    except Exception:
        return None
