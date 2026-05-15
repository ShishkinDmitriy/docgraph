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
from rdflib.namespace import RDFS, XSD

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


# Template namespaces — bound on the templates Graph so the serialized
# TTL uses CURIEs (`lis14tpl:Foo` not `<http://example.org/.../Foo>`).
_TPL      = Namespace("http://example.org/docgraph/template#")
_LIS14TPL = Namespace("http://example.org/docgraph/lis14tpl#")
_LIS      = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")


def walk_templates(
    extract_graph:  Graph,
    *,
    extracted:      list[ExtractedEntity],
    ontology:       Graph,
    base_ns:        Namespace,
    markdown:       str,
    client:         LLMClient,
    model:          ModelConfig,
    console=None,
) -> Graph:
    """Template recognition phase — produces a dedicated Graph holding
    the full template-phase contribution (lifted invocations + any
    lowered triples newly added by the LLM-confirm loop).

    The returned graph is meant to be serialized as `<slug>.templates.ttl`
    alongside extract.ttl + convert.ttl, so reviewers can see what the
    template phase asserted independently of the mega-walker's output.

    Note: the LLM-confirm loop also mutates *extract_graph* in place to
    add NEW lowered triples (so iteration N+1's SPARQL can see iteration
    N's confirmations). The returned graph re-emits those triples too,
    plus the lifted forms — the templates file is self-contained and
    the union view across all per-doc graphs sees no duplication that
    matters.

    Two sub-phases:
      1. SPARQL recognition — fully-bound patterns from the existing
         binary properties; emits the lifted form only (the lowered
         form is already in extract_graph).
      2. Batched LLM-confirm — for partial matches (one missing required
         slot), batched prompt per iteration; on confirmation, lifted
         + new lowered land here AND in extract_graph (for next iter).
    """
    g = Graph()
    g.bind("ext",      Namespace("http://example.org/docgraph/ext#"))
    g.bind("tpl",      _TPL)
    g.bind("lis14tpl", _LIS14TPL)
    g.bind("lis",      _LIS)
    g.bind("rdfs",     RDFS)
    g.bind("xsd",      XSD)
    g.bind("ex",       base_ns)

    # ── Sub-phase 1: SPARQL recognition (mechanical, no LLM) ──
    recognized = recognize_invocations(extract_graph, base_ns=base_ns)
    if recognized:
        for triple in materialize_recognized(recognized, base_ns=base_ns):
            g.add(triple)
        if console:
            console.print(f"  recognized {len(recognized)} template invocation(s) "
                          f"from existing triples ({len(g)} lifted triple(s))")

    # ── Sub-phase 2: batched-loop LLM-confirm partial matches ──
    if extracted:
        confirmed = confirm_loop(
            extract_graph, markdown=markdown, extracted=extracted,
            ontology=ontology, client=client, model=model, base_ns=base_ns,
            console=console,
        )
        if len(confirmed) > 0:
            for triple in confirmed:
                g.add(triple)
            if console:
                console.print(f"    [dim]+{len(confirmed)} triple(s) (lifted + new lowered) "
                              f"from confirmed partial matches[/dim]")

    return g


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


_BATCHED_CONFIRM_PROMPT = """\
You are confirming whether several knowledge-graph patterns apply in this
document. Each numbered question is a partial pattern match: most slots are
already filled, ONE slot is missing, and we need to know its value.

For each question, answer with:
  - the EXACT name of an extracted entity from the candidate list (preferred
    when the slot expects an entity), OR
  - a literal value (when the slot expects one — number, date, string), OR
  - "none" when the document doesn't support filling the slot.

Be conservative — answer "none" rather than guess.

== Already-extracted entities (you may bind to one of these by name) ==

{candidates_block}

== Document (markdown view, anchors `{{#id-N}}`) ==

\"\"\"
{markdown}
\"\"\"

== Questions ==

{questions_block}

Reply with JSON only — one entry per question id:

{{
  "Q1": "<entity name OR literal OR 'none'>",
  "Q2": "...",
  ...
}}
"""


def confirm_loop(
    graph:           Graph,
    *,
    markdown:        str,
    extracted:       list[ExtractedEntity],
    ontology:        Graph,
    client:          LLMClient,
    model:           ModelConfig,
    base_ns:         Namespace | None = None,
    max_iterations:  int = 5,
    max_questions_per_iter: int = 20,
    console=None,
) -> Graph:
    """Iterative batched LLM-confirm of partial template matches.

    Each iteration:
      1. Find partial matches via SPARQL (one missing required slot each).
      2. Shortlist: drop questions we asked previously; sort by slot-
         completeness (more bound = more likely real); cap to
         max_questions_per_iter to bound prompt size.
      3. Single LLM call covering all shortlist questions.
      4. Parse `{Qid: answer}` JSON; for answers that resolve, materialize
         the now-complete invocation (lifted + lowered triples).
      5. If new triples landed, loop — they may unlock other patterns.

    Terminates on: no partials, no new questions (everything asked already),
    no new triples, or max_iterations reached.
    """
    out = Graph()
    asked: set = set()

    for iteration in range(max_iterations):
        partials = partial_match_invocations(graph)
        if not partials:
            break

        shortlist = []
        for p in partials:
            key = _question_key(p)
            if key in asked:
                continue
            shortlist.append((key, p))
        if not shortlist:
            break
        # Most-bound first — those are likeliest to be real.
        shortlist.sort(key=lambda kp: -len(kp[1].known_bindings))
        shortlist = shortlist[:max_questions_per_iter]

        for key, _ in shortlist:
            asked.add(key)

        questions = [(f"Q{i+1}", p) for i, (_, p) in enumerate(shortlist)]
        if console:
            console.print(f"  iter {iteration+1}: {len(questions)} partial match(es), one batched LLM call...")
        answers = _batched_confirm_call(
            questions, markdown=markdown, extracted=extracted, ontology=ontology,
            client=client, model=model,
        )

        new_triples_count = 0
        for qid, partial in questions:
            answer_str = (answers.get(qid) or "").strip()
            if not answer_str or answer_str.lower() == "none":
                continue
            slot = partial.template.slot(partial.missing_slot)
            if slot is None:
                continue
            binding = _resolve_answer(answer_str, slot, extracted)
            if binding is None:
                continue
            complete = dict(partial.known_bindings)
            complete[partial.missing_slot] = binding
            try:
                lifted_g  = materialize_lifted(partial.template, complete, ext_ns=base_ns)
                lowered_g = expand(partial.template, complete, ext_ns=base_ns)
            except Exception as exc:                # pragma: no cover
                logger.warning("confirm_loop: materialize failed for %s: %s",
                               partial.template.uri, exc)
                continue
            for triple in lifted_g:
                out.add(triple)
                if triple not in graph:
                    graph.add(triple)
                    new_triples_count += 1
            for triple in lowered_g:
                out.add(triple)
                if triple not in graph:
                    graph.add(triple)
                    new_triples_count += 1
            if console:
                tmpl_label = partial.template.label or partial.template.slug
                console.print(f"    [dim]confirmed {tmpl_label}: "
                              f"{partial.missing_slot} = {answer_str}[/dim]")

        if new_triples_count == 0:
            break

    return out


def _question_key(partial: PartialMatch) -> tuple:
    """Stable key identifying a partial-match question — never re-ask the
    same question across loop iterations even if it resurfaces."""
    bindings_key = frozenset(
        (k, str(v)) for k, v in partial.known_bindings.items()
    )
    return (str(partial.template.uri), bindings_key, partial.missing_slot)


def _batched_confirm_call(
    questions:  list[tuple[str, PartialMatch]],
    *,
    markdown:   str,
    extracted:  list[ExtractedEntity],
    ontology:   Graph,
    client:     LLMClient,
    model:      ModelConfig,
) -> dict[str, str]:
    """Build + send the single batched prompt; return {Qid: answer_str}."""
    blocks = []
    for qid, p in questions:
        slot = p.template.slot(p.missing_slot)
        slot_desc  = p.template.var_descriptions.get(p.missing_slot, "(no description)")
        slot_range = _label_for_range(slot.range, ontology) if slot else "(any)"
        known_block = _format_known_bindings(p.known_bindings, extracted, ontology)
        blocks.append(
            f"{qid}. Pattern: {p.template.label or p.template.slug}\n"
            f"    Definition: {p.template.definition or '(no definition)'}\n"
            f"    Already bound:\n{known_block}\n"
            f"    Missing: `{p.missing_slot}` ({slot_range}) — {slot_desc}\n"
        )

    candidates = _format_all_candidate_entities(extracted)
    prompt = _BATCHED_CONFIRM_PROMPT.format(
        candidates_block = candidates,
        markdown         = markdown,
        questions_block  = "\n".join(blocks),
    )
    meta = f"{model.model_id}  template confirm: batched ({len(questions)} q's)"
    log_prompt("part14/template-confirm", prompt, logger=logger, metadata=meta)
    resp = client.create(
        model_id = model.model_id,
        messages = [{"role": "user", "content": prompt}],
        max_tokens = 2048,
    )
    text = "".join(b.text for b in resp.content if isinstance(b, TextBlock)).strip()
    log_response("part14/template-confirm", text, logger=logger, metadata=meta, as_json=True)
    return _parse_batched_response(text, qids=[qid for qid, _ in questions])


def _parse_batched_response(text: str, *, qids: list[str]) -> dict[str, str]:
    """Parse {Qid: answer} JSON from the LLM, tolerant of code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        payload = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for qid in qids:
        v = payload.get(qid)
        if v is None:
            continue
        out[qid] = str(v).strip()
    return out


def _format_all_candidate_entities(extracted: list[ExtractedEntity]) -> str:
    """List every extracted entity with its types — the batched prompt is
    too large to filter per-question, so we hand the LLM the full set and
    let it pick the right one per question."""
    if not extracted:
        return "  (no extracted entities yet)"
    lines = []
    for e in sorted(extracted, key=lambda x: x.label.casefold()):
        types = e.types or [e.type_uri]
        type_str = ", ".join(_local(t) for t in types)
        lines.append(f"  - {e.label}  ({type_str})")
    return "\n".join(lines)


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
