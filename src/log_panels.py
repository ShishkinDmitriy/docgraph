"""Rich-formatted debug panels for LLM prompts and responses.

Shared by classify.py, extract.py, and classifier.py so panels look identical
across stages. No-ops when the calling module's logger isn't at DEBUG level
(panels go to stderr; normal user output stays on stdout).

Style:
  - cyan border for outbound prompts (we → LLM)
  - green border for inbound responses (LLM → us)
"""

import logging

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

_console = Console(stderr=True)


def log_prompt(stage: str, prompt: str, *, logger: logging.Logger,
               metadata: str | None = None) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    title = f"[bold cyan]→ LLM[/bold cyan]  {stage}"
    if metadata:
        title += f"  [dim]{metadata}[/dim]"
    _console.print(Panel(prompt, title=title, border_style="cyan", title_align="left"))


def log_response(stage: str, response: str, *, logger: logging.Logger,
                 metadata: str | None = None, as_json: bool = False) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    title = f"[bold green]LLM →[/bold green]  {stage}"
    if metadata:
        title += f"  [dim]{metadata}[/dim]"
    if as_json:
        body = Syntax(_strip_code_fence(response), "json", theme="monokai", word_wrap=True)
    else:
        body = response
    _console.print(Panel(body, title=title, border_style="green", title_align="left"))


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
