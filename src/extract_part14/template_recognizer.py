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

import json
import logging
from dataclasses import dataclass

from rdflib import Graph, Literal, Namespace, URIRef

from src.extract_part14 import axioms
from src.extract_part14.walker import ExtractedEntity
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig
from src.templates.expand import expand, materialize_lifted
from src.templates.loader import Template
from src.templates.recognize import (
    _build_namespace_manager,
    _is_var,
    _serialize_term,
    _sparql_var_name,
    recognize,
)
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


# ── Partial-match detection (one required slot missing) ─────────────────


@dataclass
class PartialMatch:
    """A graph match where N-1 required slots are filled but ONE is missing.

    Surfaces the natural "almost-a-template" shape so the LLM can be asked
    a focused question: given these bound entities, what value belongs in
    the missing slot? Cheaper and more accurate than letting the LLM
    enumerate template candidates from scratch in the mega-call.
    """
    template:        Template
    known_bindings:  dict           # slot_name → URIRef | Literal
    missing_slot:    str            # name of the one required slot with no binding


def partial_match_invocations(graph: Graph) -> list[PartialMatch]:
    """For every registered template, find graphs missing exactly ONE
    required slot.

    For each template T with required slots {r1, …, rN}, run N "drop-one"
    SPARQL queries — each omits the lowered triples that mention one
    required slot's variable. Each query result row binds the OTHER
    (N-1) required slots, plus any optional slots whose triples do
    happen to match. Each row → one PartialMatch.
    """
    out: list[PartialMatch] = []
    for template in default_registry().all():
        if not template.lowered or not template.is_instance_form:
            continue
        # Skip partial matches that are subsumed by a fully-recognized
        # invocation — i.e. the same role/player bindings already appear
        # in a complete match where activity was also present. Otherwise
        # we'd emit "partial: missing X" right next to the full bind.
        already_full = list(recognize(template, graph))
        required_slots = [s.name for s in template.slots if s.min_count > 0]
        for missing in required_slots:
            try:
                rows = _query_with_slot_dropped(template, graph, missing)
            except Exception as exc:                # pragma: no cover
                logger.warning("partial_match: %s missing %s SPARQL failed: %s",
                               template.uri, missing, exc)
                continue
            for row in rows:
                known = {k: v for k, v in row.asdict().items() if v is not None}
                # Filling `missing` only completes the pattern if every OTHER
                # required slot is already bound by the SPARQL row. Skip the
                # partial otherwise — materialization would fail downstream.
                still_missing = [s for s in required_slots
                                 if s != missing and s not in known]
                if still_missing:
                    continue
                if _subsumed_by_full(known, already_full):
                    continue
                out.append(PartialMatch(
                    template=template, known_bindings=known, missing_slot=missing,
                ))
    return out


def _subsumed_by_full(partial: dict, full_matches: list[dict]) -> bool:
    """True if any full match's bindings include all of *partial*'s."""
    for full in full_matches:
        if all(full.get(k) == v for k, v in partial.items()):
            return True
    return False


def _query_with_slot_dropped(template: Template, graph: Graph,
                             dropped_slot: str) -> list:
    """Run `template.lowered` as SPARQL with the dropped slot's triples
    removed — leaves only the (N-1) other required-slot triples."""
    nm = _build_namespace_manager(template)
    used_prefixes: set[str] = set()
    drop_uri = URIRef(str(template.var_ns) + dropped_slot)

    bgp_lines = []
    for s, p, o in sorted(template.lowered,
                          key=lambda t: (str(t[0]), str(t[1]), str(t[2]))):
        if drop_uri in (s, p, o):
            continue            # this triple references the dropped slot — omit it
        bgp_lines.append(
            f"  {_serialize_term(s, template, nm, used_prefixes)} "
            f"{_serialize_term(p, template, nm, used_prefixes)} "
            f"{_serialize_term(o, template, nm, used_prefixes)} ."
        )
    if not bgp_lines:
        return []           # nothing left to match — every triple referenced the slot

    prefix_lines = [
        f"PREFIX {pfx}: <{template.prefixes[pfx]}>"
        for pfx in sorted(used_prefixes) if pfx in template.prefixes
    ]
    sparql = (("\n".join(prefix_lines) + "\n\n") if prefix_lines else "") \
             + "SELECT * WHERE {\n" + "\n".join(bgp_lines) + "\n}"
    return list(graph.query(sparql))


# ── LLM-confirm pass for partial matches ────────────────────────────────


_CONFIRM_PROMPT = """\
You are confirming whether a knowledge-graph pattern applies in this document.

Pattern: {template_label}
Description: {template_definition}

Already extracted (matching most of the pattern):
{known_block}

Missing slot: `{missing_name}`
  Description: {missing_description}
  Range:       {missing_range}

The document below is the source. Your task: from the document, identify the
value of the missing slot — must be either the EXACT name of an already-
extracted entity (preferred) or a literal of the right type. If no value is
present in the document, answer "none".

== Already-extracted entities (you may bind to one of these by name) ==

{candidate_entities_block}

== Document (markdown view, anchors `{{#id-N}}`) ==

\"\"\"
{markdown}
\"\"\"

Reply with JSON only, no prose:

{{"answer":   "<entity name OR literal value OR 'none'>",
  "evidence": "<verbatim quote from the document supporting the answer, or empty>"}}
"""


def confirm_partial_matches(
    matches:    list[PartialMatch],
    *,
    markdown:   str,
    extracted:  list[ExtractedEntity],
    ontology:   Graph,
    client:     LLMClient,
    model:      ModelConfig,
    base_ns:    Namespace | None = None,
    console=None,
) -> Graph:
    """Ask the LLM to fill the missing slot for each partial match.

    Returns a Graph containing both the lifted form and the lowered triples
    for each confirmed invocation (the lowered triples include the NEW
    missing-slot triples we just learned about — that's the value-add).
    Confirmation is per-match (one focused LLM call each); skipped if the
    LLM answers "none" or returns an unresolvable value.
    """
    out = Graph()
    for match in matches:
        binding = _confirm_one(match, markdown=markdown, extracted=extracted,
                               ontology=ontology, client=client, model=model)
        if binding is None:
            continue
        complete = dict(match.known_bindings)
        complete[match.missing_slot] = binding
        try:
            lifted_g  = materialize_lifted(match.template, complete, ext_ns=base_ns)
            lowered_g = expand(match.template, complete, ext_ns=base_ns)
        except Exception as exc:                # pragma: no cover
            logger.warning("partial_confirm: materialize failed for %s: %s",
                           match.template.uri, exc)
            continue
        for triple in lifted_g:
            out.add(triple)
        for triple in lowered_g:
            out.add(triple)
        if console:
            tmpl_label = match.template.label or match.template.slug
            console.print(f"  [dim]LLM-confirmed {tmpl_label}: "
                          f"{match.missing_slot} = {binding!s}[/dim]")
    return out


def _confirm_one(
    match:      PartialMatch,
    *,
    markdown:   str,
    extracted:  list[ExtractedEntity],
    ontology:   Graph,
    client:     LLMClient,
    model:      ModelConfig,
) -> URIRef | Literal | None:
    """One focused LLM call to confirm the missing slot's value. Returns
    a URIRef (when matched to an extracted entity) or a typed Literal
    (when the slot expects one), or None to skip."""
    template = match.template
    missing  = template.slot(match.missing_slot)
    if missing is None:
        return None
    missing_desc  = template.var_descriptions.get(match.missing_slot, "(no description)")
    missing_range = _label_for_range(missing.range, ontology)
    known_block = _format_known_bindings(match.known_bindings, extracted, ontology)
    candidates  = _format_candidate_entities(extracted, missing.range, ontology)

    prompt = _CONFIRM_PROMPT.format(
        template_label      = template.label or template.slug,
        template_definition = template.definition or "(no definition)",
        known_block         = known_block,
        missing_name        = match.missing_slot,
        missing_description = missing_desc,
        missing_range       = missing_range,
        candidate_entities_block = candidates,
        markdown            = markdown,
    )
    meta = f"{model.model_id}  template confirm: {template.slug}/{match.missing_slot}"
    log_prompt("part14/template-confirm", prompt, logger=logger, metadata=meta)
    resp = client.create(
        model_id=model.model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=1024,
    )
    text = "".join(b.text for b in resp.content if isinstance(b, TextBlock)).strip()
    log_response("part14/template-confirm", text, logger=logger, metadata=meta, as_json=True)

    answer = _parse_confirm_response(text)
    if not answer or answer.lower() == "none":
        return None
    return _resolve_answer(answer, missing, extracted)


def _parse_confirm_response(text: str) -> str:
    """Extract the `answer` string from the LLM's JSON response. Tolerant
    of code fences and surrounding prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        return ""
    try:
        payload = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return ""
    return str(payload.get("answer", "") or "").strip()


def _resolve_answer(answer: str, slot, extracted: list[ExtractedEntity]) -> URIRef | Literal | None:
    """Map the LLM's answer string to a URIRef (matching an extracted
    entity by label) or a typed Literal (when the slot expects a literal)."""
    if slot.is_literal:
        # Coerce to typed literal using the slot's range (xsd:double, etc.)
        from src.extract_part14.property_walker import coerce_literal
        try:
            return coerce_literal(answer, slot.range)
        except Exception:
            return Literal(answer)
    # Object-valued: case-insensitive label match against extracted entities
    needle = answer.casefold()
    for e in extracted:
        if e.label.casefold() == needle:
            return e.uri
    logger.info("partial_confirm: answer %r doesn't match any extracted entity label",
                answer)
    return None


def _format_known_bindings(bindings: dict, extracted: list[ExtractedEntity],
                           ontology: Graph) -> str:
    """Render the already-bound slots as bullet lines for the prompt."""
    if not bindings:
        return "  (none)"
    lines = []
    for name, term in sorted(bindings.items(), key=lambda kv: kv[0]):
        if isinstance(term, URIRef):
            ent = next((e for e in extracted if e.uri == term), None)
            if ent is not None:
                tlabel = ", ".join(_local(t) for t in (ent.types or [ent.type_uri])) or "(?)"
                lines.append(f"  - {name}: \"{ent.label}\" (typed {tlabel})")
            else:
                lines.append(f"  - {name}: <{term}>")
        elif isinstance(term, Literal):
            lines.append(f"  - {name}: {term.toPython()!r}")
        else:
            lines.append(f"  - {name}: {term!r}")
    return "\n".join(lines)


def _format_candidate_entities(extracted: list[ExtractedEntity],
                                range_uri: URIRef | None,
                                ontology: Graph) -> str:
    """List extracted entities whose type-set is compatible with `range_uri`.
    The LLM's answer must come from this list (or be 'none')."""
    if not extracted:
        return "  (no extracted entities yet)"
    lines = []
    for e in extracted:
        types = e.types or [e.type_uri]
        if range_uri is not None:
            if not any(_is_or_subclass(t, range_uri, ontology) for t in types):
                continue
        type_str = ", ".join(_local(t) for t in types)
        lines.append(f"  - {e.label}  ({type_str})")
    return "\n".join(lines) if lines else "  (no entities of compatible type)"


def _is_or_subclass(cls: URIRef, ancestor: URIRef, ontology: Graph) -> bool:
    if cls == ancestor:
        return True
    return ancestor in axioms.superclasses(ontology, cls, direct=False)


def _local(uri) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s


def _label_for_range(range_uri: URIRef | None, ontology: Graph) -> str:
    if range_uri is None:
        return "(any)"
    return _local(range_uri)


# ── Required-slot guard (used by recognize_invocations) ─────────────────


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
