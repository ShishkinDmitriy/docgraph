"""Shared types + URI / quote minting for the part14 pipeline.

This module used to host the per-branch combined walker (`walk_branches`).
That walker has been superseded by the three-pass root-walker model — see
`root_walker.py`. What remains here are the small primitives that callers
across the pipeline still need:

  - `DG`, `LIS`, `OA` — namespace constants used everywhere.
  - `EvidenceSelector` — small dataclass carrying the LLM's evidence text
    + the HTML anchor it cited.
  - `ExtractedEntity` — per-entity record carried between passes. Supports
    multi-typing (Part 14 §E.8) via `.types`, plus LLM-supplied type hints
    consumed by enrich.
  - `mint_entity_uri` / `slug` — stable URI slugging from an entity name.

The previous `mint_quote` helper (and the `dg:Quote + oa:hasSelector`
clusters it produced) has been removed in favor of fragment-URI citations:
each evidence link in the graph is `?entity lis:representedBy <doc#id-N>`
where `id-N` is the HTML element ID seeded at conversion. See
`docs/architecture/html-pipeline.md`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from rdflib import Namespace, URIRef  # noqa: F401  (Namespace re-exported)

DG  = Namespace("urn:docgraph:vocab:meta#")
LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")
OA  = Namespace("http://www.w3.org/ns/oa#")


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class EvidenceSelector:
    """One piece of evidence the LLM cited for an entity.

    `exact` is the verbatim cited text (used for in-memory text matching,
    e.g. `infer_cross_entity_links`). `anchor` is the `id-N` of the HTML
    element the LLM identified as the source — used to mint the
    `lis:representedBy <doc#id-N>` triple in the extract graph.
    """
    exact:  str
    anchor: str = ""


@dataclass
class ExtractedEntity:
    uri:        URIRef
    type_uri:   URIRef                               # primary type (slug source, property lookup anchor)
    label:      str
    evidence:   list[EvidenceSelector] = field(default_factory=list)
    types:      list[URIRef] = field(default_factory=list)
    """All `rdf:type` triples for this entity (Part 14 §E.8 multi-typing).
    `type_uri` is one of these and is the "primary" type used for URI
    slugging and downstream property-candidate lookup. Empty list means
    only `type_uri` applies — single-typed entity, the legacy case."""
    type_hints: list[str] = field(default_factory=list)
    """LLM-suggested specific class names (e.g., "Patient", "Dentist") used
    by enrich to probe external RDLs in addition to the bare label.
    Carried as `dg:typeHint` literals on the entity in the serialized graph."""


# ── Citation URIs (fragment-into-HTML) ─────────────────────────────────────

def mint_fragment_uri(doc_uri: URIRef, anchor: str) -> URIRef:
    """Mint a fragment URI pointing at HTML element `anchor` inside `doc_uri`.

    Standard URL fragment syntax: `<doc.html#id-7>`. Browsers and URI
    libraries handle this natively. The HTML element with `id="id-7"` is
    the citation target — no `dg:Quote` indirection, no `oa:hasSelector`
    cluster, no text matching at query time.
    """
    safe = anchor.lstrip("#").strip()
    return URIRef(f"{doc_uri}#{safe}")


# ── Entity URI minting ─────────────────────────────────────────────────────

_SLUG_RX = re.compile(r"[^a-z0-9]+")


def slug(name: str, *, max_len: int = 64) -> str:
    """Normalize *name* to a Turtle-prefix-friendly local name.

    Lowercase, non-alphanumerics collapsed to hyphens, capped at *max_len*.
    No slashes — so rdflib's Turtle serializer can render the URI with the
    bound prefix instead of falling back to the long-form `<...>`. Empty
    after normalization → deterministic hash-based fallback.
    """
    s = _SLUG_RX.sub("-", name.lower()).strip("-")[:max_len]
    if not s:
        s = "anon-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return s


def mint_entity_uri(entity_name: str, base_ns: Namespace) -> URIRef:
    """Mint a stable, single-namespace entity URI: `<base_ns><slug(name)>`.

    All extracted entities live directly under *base_ns* — no per-type
    sub-paths — so the bound `ex:` prefix renders cleanly in Turtle.
    """
    return URIRef(base_ns[slug(entity_name)])
