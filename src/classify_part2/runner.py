"""Generic per-prompt LLM runner.

One shape across all 13 follow-up prompts (#2-#14): load the prompt
body from `docs/classify_prompts/`, fill placeholder variables from the
shared ``ConversionContext``, call the LLM, log prompt + response via
``log_panels`` (panels render only when the calling logger is at DEBUG),
and return the parsed JSON.

Prompt #1 (nature scan) has its own runner in ``nature_scan.py``
because its result needs special post-processing (coverage metrics +
gating decisions).
"""

from __future__ import annotations

import logging

from src.classifier import _parse_json_response
from src.classify_part2 import prompts
from src.classify_part2.context import ConversionContext, EntityRef
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)


def run(
    prompt_name: str,
    *,
    markdown: str,
    ctx: ConversionContext,
    client,
    model: ModelConfig,
    max_tokens: int = 4096,
) -> dict:
    """Run one prompt and return its parsed JSON output."""
    template = prompts.load(prompt_name)
    body = _fill(template, prompt_name, markdown, ctx)

    meta = f"{model.model_id}  max_tokens={max_tokens}  prompt={prompt_name}"
    log_prompt(f"classify/{prompt_name}", body, logger=logger, metadata=meta)
    response = client.create(
        model_id=model.model_id,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": body}],
    )
    raw = response.content[0].text
    log_response(f"classify/{prompt_name}", raw, logger=logger, metadata=meta, as_json=True)

    return _parse_json_response(raw)


# ── Placeholder filling ──────────────────────────────────────────────────────


def _fill(template: str, prompt_name: str, markdown: str, ctx: ConversionContext) -> str:
    """Substitute every ``{placeholder}`` in *template*.

    Unused placeholders for a given prompt are simply left out by the
    template; only the ones the prompt actually needs are filled.
    """
    subs = {
        "markdown":          markdown,
        "doc_kind":          ctx.doc_kind or "(unspecified)",
        "primary_subjects":  _format_list(ctx.primary_subjects),
        "activity_ids_and_labels":              _table(ctx.by_kind("activity"),
                                                       columns=("id", "label")),
        "activity_id_label_summary_table":      _table(ctx.by_kind("activity"),
                                                       columns=("id", "label", "summary")),
        "individual_id_label_kind_table":       _table(ctx.by_kind("individual"),
                                                       columns=("id", "label", "subkind")),
        "class_of_activity_id_label_table":     _table(ctx.by_kind("class_of_activity"),
                                                       columns=("id", "label")),
        "class_of_individual_id_label_table":   _table(ctx.by_kind("class_of_individual"),
                                                       columns=("id", "label")),
        "role_id_label_table":                  _table(ctx.by_kind("role"),
                                                       columns=("id", "label")),
    }
    out = template
    for key, val in subs.items():
        out = out.replace("{" + key + "}", val)
    return out


def _format_list(items: list[str]) -> str:
    if not items:
        return "(none extracted)"
    return ", ".join(items)


_HEADER = {
    "id":      "id",
    "label":   "label",
    "summary": "summary",
    "subkind": "kind",
}


def _table(refs: list[EntityRef], *, columns: tuple[str, ...]) -> str:
    """Render an EntityRef list as a markdown-ish table for the prompt context."""
    if not refs:
        return "(none extracted)"
    header = " | ".join(_HEADER.get(c, c) for c in columns)
    sep    = " | ".join("-" * max(len(_HEADER.get(c, c)), 3) for c in columns)
    rows   = [header, sep]
    for r in refs:
        cells = [_cell(getattr(r, c, "")) for c in columns]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def _cell(value) -> str:
    s = str(value or "")
    s = s.replace("\n", " ").replace("|", "/").strip()
    if len(s) > 120:
        s = s[:117] + "..."
    return s or "—"
