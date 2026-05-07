"""Materialise LLM-emitted template instances into the per-doc graph.

Storage shape is the **lifted form** — the compact `var:this a tpl:Foo ;
<slot> ?value` triples produced by `materialize_lifted`. The lowered Part
2 cluster is recoverable on demand via `templates.expand` against the
same bindings.

LLM responses for template-aware prompts include an `instances` key:

    {
      "instances": [
        {
          "template": "iso:ClassificationOfIndividual",
          "bindings": {
            "hasClassified":    "ext:p-101",
            "hasClassifier":    "ext:centrifugal-pump",
            "valEffectiveDate": "2021-07-18T13:59:00Z"
          }
        },
        ...
      ],
      ...
    }

This module turns that list into a single rdflib Graph by looking each
template up in the registry and expanding via `src.templates.expand`. URIs
in `bindings` may be CURIEs (`ext:p-101`, `iso:Foo`) — they're resolved
against the registry's per-template prefix table plus the document's
`ext:` namespace.

Unknown templates and bad bindings are logged and skipped rather than
raising; the rest of the response (raw fallback section) still proceeds.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import XSD

from src.templates.expand import materialize_lifted
from src.templates.loader import Template

if TYPE_CHECKING:
    from src.classify_part2.context import EntityRef
    from src.templates.registry import Registry

logger = logging.getLogger(__name__)


def _resolve_curie(
    value: str,
    template: Template,
    ext_ns,
    entities: "dict[str, EntityRef]",
    *,
    datatype: URIRef | None = None,
) -> URIRef | Literal:
    """Resolve a string emitted by the LLM into an RDF term.

    `prefix:local` shapes:
    - `ext:<id>` first looks `<id>` up in `entities` (the per-doc registry of
      already-extracted entities, keyed by their LLM-facing slug). When found,
      returns the EntityRef's actual minted URI — which carries the
      kind-prefix (`act-`, `ind-`, `cls-`, …) the rest of the graph uses, so
      template instances link to existing activities/individuals/classes
      instead of dangling. When no match, mints `ext_ns + local` as a fresh
      URI (the LLM has introduced a new ad-hoc class/scale).
    - Other prefixes resolve via the template's `@prefix` table (`iso:`,
      `rdl:`, …).

    `datatype` is the xsd type the slot expects (xsd:decimal, xsd:dateTime,
    …) when the lowered body declares one structurally. Non-URI values are
    typed against it so downstream consumers see typed numerics/dates rather
    than plain strings.
    """
    if not isinstance(value, str):
        return Literal(value, datatype=datatype) if datatype else Literal(value)
    if ":" in value and not value.startswith(("http://", "https://", "urn:")):
        prefix, local = value.split(":", 1)
        if prefix == "ext":
            existing = entities.get(local)
            if existing is not None:
                return existing.uri
            return URIRef(str(ext_ns) + local)
        ns = template.prefixes.get(prefix)
        if ns:
            return URIRef(ns + local)
        # Unknown prefix — fall through; the engine will treat as a literal.
    if value.startswith(("http://", "https://", "urn:")):
        return URIRef(value)
    return Literal(value, datatype=datatype) if datatype else Literal(value)


def _coerce_bindings(
    raw: dict,
    template: Template,
    ext_ns,
    entities: "dict[str, EntityRef]",
) -> dict:
    """Resolve every binding value via `_resolve_curie`. Lists pass through
    element-wise (multi-valued slots). Per-slot xsd datatype (when the
    lowered body declares one) is applied to literal-shaped values."""
    out: dict = {}
    for key, val in raw.items():
        dt = template.var_datatypes.get(key)
        if isinstance(val, list):
            out[key] = [
                _resolve_curie(v, template, ext_ns, entities, datatype=dt)
                for v in val
            ]
        else:
            out[key] = _resolve_curie(
                val, template, ext_ns, entities, datatype=dt
            )
    return out


def _resolve_template_uri(value: str, registry: "Registry") -> Template | None:
    """Look the LLM-supplied template identifier up in the registry.

    Accepts full URI (`http://…/Foo`) or CURIE (`iso:Foo`). The CURIE form
    is resolved against the prefix tables of every registered template —
    if any registered template has `iso:` mapping that produces this URI,
    we match. This keeps prompt-emit ergonomic without forcing the LLM to
    use absolute URIs.
    """
    if value in (str(uri) for uri in registry.by_uri):
        return registry.by_uri[URIRef(value)]
    direct = URIRef(value)
    if direct in registry.by_uri:
        return registry.by_uri[direct]
    if ":" in value and not value.startswith(("http://", "https://", "urn:")):
        prefix, local = value.split(":", 1)
        for tpl in registry.all():
            ns = tpl.prefixes.get(prefix)
            if ns and URIRef(ns + local) in registry.by_uri:
                return registry.by_uri[URIRef(ns + local)]
    return None


def expand_instances(
    instances: list[dict],
    registry: "Registry",
    *,
    ext_ns,
    entities: "dict[str, EntityRef] | None" = None,
) -> Graph:
    """Expand every `{template, bindings}` instance into a merged Graph.

    `entities` is the per-doc registry of already-extracted entities (typically
    `ctx.entities`). When provided, `ext:<id>` bindings prefer the existing
    EntityRef's URI over minting a new one — wiring template instances to
    activities/individuals/classes the earlier prompts produced.

    Unknown templates / malformed entries are logged and skipped; the
    function never raises on bad LLM output.
    """
    g = Graph()
    if not instances:
        return g
    entities = entities or {}
    for idx, inst in enumerate(instances):
        if not isinstance(inst, dict):
            logger.warning("template instance #%d is not a dict: %r", idx, inst)
            continue
        tpl_id = inst.get("template")
        bindings_raw = inst.get("bindings")
        if not tpl_id or not isinstance(bindings_raw, dict):
            logger.warning(
                "template instance #%d missing template or bindings: %r",
                idx, inst,
            )
            continue
        tpl = _resolve_template_uri(tpl_id, registry)
        if tpl is None:
            logger.warning(
                "template %r not found in registry; skipping", tpl_id
            )
            continue
        try:
            bindings = _coerce_bindings(bindings_raw, tpl, ext_ns, entities)
            sub_g = materialize_lifted(tpl, bindings, ext_ns=ext_ns)
        except Exception as exc:
            logger.warning(
                "materialising %s failed: %s; bindings=%r",
                tpl.uri, exc, bindings_raw,
            )
            continue
        g += sub_g
    return g
