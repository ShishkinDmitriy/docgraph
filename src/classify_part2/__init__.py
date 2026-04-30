"""ISO 15926-2 (Part 2) classification pipeline.

Replaces the old single-call Part 14 classifier (`src/classify.py`) with a
14-prompt pipeline that builds a Part 2 entity graph for each document.

See `docs/classify_design.md` for the high-level design and gating logic;
each prompt's body lives in `docs/classify_prompts/NN_*.md`.
"""

from src.classify_part2.ns import DG, ISO15926, EXT_NS_FOR
from src.classify_part2.uri import slugify, mint_ext

__all__ = ["DG", "ISO15926", "EXT_NS_FOR", "slugify", "mint_ext"]
