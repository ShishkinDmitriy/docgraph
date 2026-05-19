"""Debug logging for LLM prompts and responses.

Two output modes:

  - **TTY (interactive)**: rich.Panel with colored borders. Cyan for
    outbound prompts (we → LLM), green for inbound responses.
  - **Non-TTY (file/pipe)**: plain text section dividers. No box-drawing,
    no color codes, no trailing whitespace — the output is meant to be
    grep'd, copy-pasted, and read in a plain editor.

Shared by classify.py, extract.py, classifier.py, root_walker.py,
property_walker.py so output looks identical across stages. No-ops when
the calling module's logger isn't at DEBUG level.

The TTY check uses `sys.stderr.isatty()` once at import time. To force
plain output even from a TTY (e.g., when capturing with `tee`), set
`DOCGRAPH_PLAIN_LOGS=1`.
"""

from __future__ import annotations

import logging
import os
import sys

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text


def _is_plain_mode() -> bool:
    """True when log output should be plain text instead of rich panels.

    Plain mode kicks in when stderr is not a terminal (i.e., redirected
    to a file or pipe), or when DOCGRAPH_PLAIN_LOGS is set to a truthy
    value. Saved once at import time — switching mid-run isn't supported.
    """
    if os.environ.get("DOCGRAPH_PLAIN_LOGS", "").lower() in ("1", "true", "yes"):
        return True
    try:
        return not sys.stderr.isatty()
    except Exception:
        return True


_PLAIN_MODE = _is_plain_mode()

# Rich Console for the panel path. In plain mode this is unused but kept
# instantiated so callers don't pay the cost of checking on every call.
_console = Console(stderr=True)


def log_prompt(stage: str, prompt: str, *, logger: logging.Logger,
               metadata: str | None = None) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if _PLAIN_MODE:
        _emit_plain("→ LLM", stage, prompt, metadata, as_json=False)
        return
    title = f"[bold cyan]→ LLM[/bold cyan]  {stage}"
    if metadata:
        title += f"  [dim]{metadata}[/dim]"
    # Wrap the body as Text so rich doesn't interpret markup-looking
    # substrings like `[quality]` (template slot placeholders) as malformed
    # style tags and silently strip them.
    _console.print(Panel(Text(prompt), title=title, border_style="cyan", title_align="left"))


def log_response(stage: str, response: str, *, logger: logging.Logger,
                 metadata: str | None = None, as_json: bool = False) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    if _PLAIN_MODE:
        body = _strip_code_fence(response) if as_json else response
        _emit_plain("LLM →", stage, body, metadata, as_json=as_json)
        return
    title = f"[bold green]LLM →[/bold green]  {stage}"
    if metadata:
        title += f"  [dim]{metadata}[/dim]"
    if as_json:
        body = Syntax(_strip_code_fence(response), "json", theme="monokai", word_wrap=True)
    else:
        # Same reason as log_prompt — `[slot]` placeholders that may appear
        # in the response shouldn't be mis-parsed as rich markup.
        body = Text(response)
    _console.print(Panel(body, title=title, border_style="green", title_align="left"))


def _emit_plain(direction: str, stage: str, body: str,
                metadata: str | None, *, as_json: bool) -> None:
    """Write a plain-text block: header line + body + trailing blank line.

    No color codes, no panels, no padding. Headers are easy to grep
    (`grep '^=== '`) and the body sits flush-left for clipboard friendliness.
    """
    meta_suffix = f"  {metadata}" if metadata else ""
    kind = "json" if as_json else "text"
    print(f"=== {direction}  {stage}  [{kind}]{meta_suffix}", file=sys.stderr)
    print(body.rstrip(), file=sys.stderr)
    print(f"=== end  {direction}  {stage}", file=sys.stderr)
    print("", file=sys.stderr)


def _strip_code_fence(text: str) -> str:
    """Strip ```json ... ``` markdown code fences that LLMs sometimes wrap
    JSON responses in. Same logic the parsers use, kept here so the panel
    renders clean JSON for syntax highlighting."""
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop the leading fence (with optional language tag like ```json)
    after_open = s.split("\n", 1)[1] if "\n" in s else s[3:]
    # Drop the closing fence
    if after_open.rstrip().endswith("```"):
        after_open = after_open.rstrip()[:-3]
    return after_open.strip()
