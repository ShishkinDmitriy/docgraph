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
    body = Syntax(response, "json", theme="monokai", word_wrap=True) if as_json else response
    _console.print(Panel(body, title=title, border_style="green", title_align="left"))
