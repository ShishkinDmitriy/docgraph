"""Mechanical template recognition over an extracted graph (no LLM).

After the mega-walker writes its graph, every registered template's lowered
pattern is run as a SPARQL query against that graph (`src.templates.recognize`).
Each match becomes a recognized invocation: we materialize the template's
lifted form with the matched bindings and merge it into the graph.

This catches the common case where the LLM extracted the constituent triples
of a pattern as binary properties (e.g. `<datum> lis:datumValue X ; lis:datumUOM Y`)
but didn't emit the corresponding template invocation. SPARQL recovers the
pattern; the lifted form is added so downstream consumers see one structured
fact rather than three loose triples.

Idempotent: `materialize_lifted` mints a stable hash-based anchor URI from the
slot bindings, so re-running on a graph that already contains the lifted form
produces the same triples (graphs are sets — no duplication).

Pure mechanical pass, no LLM cost. Partial-match cases (where 1-2 slots are
missing in the graph and would need LLM confirmation) are out of scope here —
they belong to a follow-up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rdflib import Graph, Namespace

from src.templates.expand import materialize_lifted
from src.templates.loader import Template
from src.templates.recognize import recognize
from src.templates.registry import default_registry

logger = logging.getLogger(__name__)


@dataclass
class RecognizedInvocation:
    """One template invocation discovered mechanically by SPARQL."""
    template: Template
    bindings: dict


def recognize_invocations(
    graph:    Graph,
    base_ns:  Namespace | None = None,
) -> list[RecognizedInvocation]:
    """Run every registered template's lowered pattern against *graph*.

    Returns one `RecognizedInvocation` per match (per template). The caller
    decides what to do with them — typically, materialize each into the
    graph via `materialize_recognized`.
    """
    out: list[RecognizedInvocation] = []
    for template in default_registry().all():
        # Skip templates that have no `lis:` or LIS-related lowered triples
        # — pattern-form templates rooted in non-LIS namespaces (e.g. PROV-O
        # bridges authored elsewhere) shouldn't fire on a Part 14 graph.
        if not template.lowered:
            continue
        try:
            matches = recognize(template, graph)
        except Exception as exc:                # pragma: no cover — guard only
            logger.warning("template_recognizer: %s SPARQL failed: %s",
                           template.uri, exc)
            continue
        for bindings in matches:
            # Skip matches with missing required slots (recognize can produce
            # incomplete dicts when SPARQL returns fewer columns than slots).
            if _has_unbound_required_slot(template, bindings):
                continue
            out.append(RecognizedInvocation(template=template, bindings=bindings))
    return out


def materialize_recognized(
    invocations: list[RecognizedInvocation],
    *,
    base_ns:     Namespace | None = None,
) -> Graph:
    """Turn each recognized invocation into its lifted-form triples.

    Returns a new Graph; merge it into the extract graph. The lifted form
    captures the invocation as a single typed instance with named slot
    triples (e.g. `<inst> a lis14tpl:Foo ; foo-slot:datum <X>`). The
    lowered triples are already in the source graph (that's what we
    matched against), so we don't re-emit them.
    """
    out = Graph()
    for inv in invocations:
        try:
            lifted = materialize_lifted(inv.template, inv.bindings, ext_ns=base_ns)
        except Exception as exc:                # pragma: no cover
            logger.warning("template_recognizer: lifted materialization failed for %s: %s",
                           inv.template.uri, exc)
            continue
        for s, p, o in lifted:
            out.add((s, p, o))
    return out


def _has_unbound_required_slot(template: Template, bindings: dict) -> bool:
    """Return True if any REQUIRED slot is missing from `bindings`.

    SPARQL `recognize` can return a row with fewer columns than the template
    has slots when an OPTIONAL slot's lowered triple isn't matched. For
    instance-form templates we treat any required slot whose key is absent
    (or value is None) as a non-recognition — better than emitting a
    half-bound lifted form.
    """
    if not template.is_instance_form:
        return False
    for slot in template.slots:
        if slot.min_count == 0:
            continue   # optional slot — absent is fine
        v = bindings.get(slot.name)
        if v is None:
            return True
    return False
