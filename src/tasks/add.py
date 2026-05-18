"""add — composite task. Empty body; just pulls every upstream.

`add_registry.run("add", ctx)` is the entry point for the full per-doc
pipeline. Its dep on `diagram` cascades through register → align →
templates → extract → load_html → convert → recognize → identity.
"""

from __future__ import annotations

from src.tasks._registry import add_registry


@add_registry.task("add", deps=("diagram",))
def add(ctx) -> None:
    pass
