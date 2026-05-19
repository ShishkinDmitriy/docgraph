"""add — composite task. Empty body; just pulls every upstream.

`docgraph.run("add", ctx)` is the entry point for the full per-doc
pipeline. Its dep on `diagram` cascades through register → align →
templates → extract → load_html → convert → recognize → identity.
"""

from __future__ import annotations

from src.tasks._registry import docgraph


@docgraph.task(deps=("diagram",))
def add(ctx) -> None:
    pass
