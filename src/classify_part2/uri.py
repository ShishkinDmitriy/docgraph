"""Slug + URI helpers.

URIs minted by the converter are deterministic: same JSON input + same
source slug → same URIs. Stability matters because cascade-delete works
by named graph; if a re-ingest produced different URIs, downstream
references would break.
"""

import re

from rdflib import Namespace, URIRef


def slugify(s: str) -> str:
    """Lowercase, hyphenated, ASCII-only. Empty input → "x"."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (s or "").strip().lower()).strip("-")
    return s or "x"


def mint_ext(ns: Namespace, *, kind: str, ident: str) -> URIRef:
    """Mint a URI like ``<ext_ns>/<kind>/<ident>``.

    *kind* is a short bucket name ("act", "ind", "cls", "role", "part",
    "wp", "tseq", "prop", "qty", "rep", "conn", "appr", "ls", "rev").
    """
    return URIRef(f"{ns}{slugify(kind)}/{slugify(ident)}")
