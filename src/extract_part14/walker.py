"""Shared types + URI / quote minting for the part14 pipeline.

This module used to host the per-branch combined walker (`walk_branches`).
That walker has been superseded by the three-pass root-walker model — see
`root_walker.py`. What remains here are the small primitives that callers
across the pipeline still need:

  - `DG`, `LIS`, `OA` — namespace constants used everywhere.
  - `EvidenceSelector` — small dataclass for an `oa:TextQuoteSelector`.
  - `ExtractedEntity` — per-entity record carried between passes. Supports
    multi-typing (Part 14 §E.8) via `.types`, plus LLM-supplied type hints
    consumed by enrich.
  - `mint_quote` — deterministic SHA-1-hashed dg:Quote with an
    oa:TextQuoteSelector. Idempotent — same exact text → same URI.
  - `mint_entity_uri` / `_entity_local` — stable URI slugging from
    (branch_label, entity_name).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF

DG  = Namespace("http://example.org/docgraph/meta#")
LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")
OA  = Namespace("http://www.w3.org/ns/oa#")


# ── Data structures ────────────────────────────────────────────────────────

@dataclass
class EvidenceSelector:
    exact:  str
    prefix: str = ""
    suffix: str = ""


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


# ── Quote minting (top-down — quotes only for cited evidence) ──────────────

def _quote_local_name(exact: str) -> str:
    """Deterministic SHA-1 of the exact text — yields cross-source dedup."""
    return "quote-" + hashlib.sha1(exact.encode("utf-8")).hexdigest()[:12]


def mint_quote(
    g: Graph,
    selector: EvidenceSelector,
    *,
    base_ns: Namespace,
    md_source_uri: URIRef,
) -> URIRef:
    """Mint a dg:Quote with an oa:TextQuoteSelector pointing into the
    markdown source. Idempotent — same text → same URI; calling twice adds
    the same triples, no duplication."""
    q_uri = URIRef(base_ns[_quote_local_name(selector.exact)])
    g.add((q_uri, RDF.type, DG.Quote))
    g.add((q_uri, RDF.type, LIS.InformationObject))
    g.add((q_uri, OA.hasSource, md_source_uri))

    sel_node = BNode()
    g.add((q_uri, OA.hasSelector, sel_node))
    g.add((sel_node, RDF.type, OA.TextQuoteSelector))
    g.add((sel_node, OA.exact, Literal(selector.exact)))
    if selector.prefix:
        g.add((sel_node, OA.prefix, Literal(selector.prefix)))
    if selector.suffix:
        g.add((sel_node, OA.suffix, Literal(selector.suffix)))

    return q_uri


# ── Entity URI minting ─────────────────────────────────────────────────────

_SLUG_RX = re.compile(r"[^a-z0-9]+")


def _entity_local(branch_label: str, name: str) -> str:
    branch_slug = _SLUG_RX.sub("-", branch_label.lower()).strip("-")[:32]
    name_slug   = _SLUG_RX.sub("-", name.lower()).strip("-")[:48]
    if not name_slug:
        name_slug = "anon-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{branch_slug}/{name_slug}"


def mint_entity_uri(branch_label: str, entity_name: str, base_ns: Namespace) -> URIRef:
    return URIRef(base_ns[_entity_local(branch_label, entity_name)])
