"""Expand a template instance into a reified Part 2 graph.

Inputs: a `Template` (loaded by `load_template`) and a `bindings` dict mapping
slot names to RDF terms (URIRef / Literal) or plain Python values that get
coerced based on the slot's range.

Output: a fresh `rdflib.Graph` containing the substituted lowered triples, with
deterministic URIs minted for intermediate variables and former blank nodes so
re-expansion of the same instance is idempotent.

Multi-valued slots are supported (≤1 per template). For triples that
"transitively touch" the multi-valued slot variable through shared intermediate
nodes, the substitution is repeated per value with fresh per-iteration URIs;
triples disconnected from the multi-valued slot are emitted once with stable
URIs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from rdflib import Graph, Literal, Namespace, URIRef

from src.templates.loader import Slot, Template


def _coerce(value, slot: Slot):
    """Turn a Python value into a Literal or URIRef given the slot's range."""
    if isinstance(value, (URIRef, Literal)):
        return value
    if slot.is_literal:
        return Literal(value, datatype=slot.range) if slot.range else Literal(
            value
        )
    return URIRef(str(value))


def _binding_hash(template_slug: str, hashable_bindings: dict) -> str:
    canon = json.dumps(hashable_bindings, sort_keys=True, default=str)
    return hashlib.sha256(
        f"{template_slug}|{canon}".encode("utf-8")
    ).hexdigest()[:16]


def _instance_ns(template_slug: str, binding_hash: str) -> Namespace:
    return Namespace(f"urn:tpl-instance/{template_slug}/{binding_hash}/")


def _is_intermediate(term, template: Template, slot_names: set[str]) -> bool:
    """A URI is an intermediate if it lives in the template's var: or anon:
    namespace and (for var:) doesn't correspond to a declared slot or `this`."""
    if not isinstance(term, URIRef):
        return False
    s = str(term)
    if s.startswith(str(template.anon_ns)):
        return True
    if s.startswith(str(template.var_ns)):
        local = s[len(str(template.var_ns)) :]
        return local not in slot_names and local != "this"
    return False


def _is_slot_var(term, template: Template, slot_names: set[str]) -> bool:
    if not isinstance(term, URIRef):
        return False
    s = str(term)
    if not s.startswith(str(template.var_ns)):
        return False
    return s[len(str(template.var_ns)) :] in slot_names


def _slot_var_local(term, template: Template) -> str:
    return str(term)[len(str(template.var_ns)) :]


def _per_iter_terms(
    template: Template, multi_var: URIRef, intermediates: set[URIRef]
) -> set[URIRef]:
    """Anon URIs (former blank nodes) reachable from the multi-valued slot
    variable through co-occurrence in lowered triples — these need fresh URIs
    per iteration.

    Named intermediate variables (in `var:` namespace) are *not* propagated
    through: they're explicit identity anchors the template author named so
    they could be shared across multiple tuples. The canonical case is
    `tpl:SourcedAssertion`'s `var:quote` — one quote shared by N
    descriptions and one composition. Anon URIs are treated as local
    structural glue: if any iteration value flows into them, the whole tuple
    is per-iteration.
    """
    anon_intermediates = {
        t for t in intermediates if str(t).startswith(str(template.anon_ns))
    }
    relevant = anon_intermediates | {multi_var}

    adj: dict[URIRef, set[URIRef]] = {t: set() for t in relevant}
    for s, _, o in template.lowered:
        in_triple = {x for x in (s, o) if x in relevant}
        for t in in_triple:
            adj[t] |= in_triple - {t}

    seen = {multi_var}
    frontier = [multi_var]
    while frontier:
        cur = frontier.pop()
        for nbr in adj[cur]:
            if nbr not in seen:
                seen.add(nbr)
                frontier.append(nbr)
    return seen & anon_intermediates


def _validate_bindings(template: Template, bindings: dict) -> None:
    if not template.is_instance_form:
        # Pattern-form templates: bindings must be keyed by variable local-name.
        return
    slot_names = {s.name for s in template.slots}
    extras = set(bindings) - slot_names
    if extras:
        raise ValueError(
            f"unknown slot(s) {sorted(extras)!r} for template "
            f"{template.uri}; declared slots: {sorted(slot_names)!r}"
        )
    for slot in template.slots:
        present = slot.name in bindings and bindings[slot.name] is not None
        if not present and slot.min_count > 0:
            raise ValueError(
                f"missing required slot {slot.name!r} for {template.uri}"
            )
        if present and not slot.is_multi:
            v = bindings[slot.name]
            if isinstance(v, (list, tuple)) and len(v) != 1:
                raise ValueError(
                    f"slot {slot.name!r} is single-valued but got "
                    f"{len(v)} values"
                )


def _coerce_all(template: Template, bindings: dict) -> dict[str, list]:
    """Return {slot_name: [coerced values...]}. Single-valued slots become
    lists of length 1."""
    out: dict[str, list] = {}
    for slot in template.slots:
        raw = bindings.get(slot.name)
        if raw is None:
            out[slot.name] = []
            continue
        if slot.is_multi:
            values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        else:
            values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        out[slot.name] = [_coerce(v, slot) for v in values]
    return out


def _multi_slot(template: Template) -> Slot | None:
    multi: list[Slot] = [s for s in template.slots if s.is_multi]
    if len(multi) > 1:
        raise ValueError(
            f"template {template.uri} has more than one multi-valued slot — "
            f"see ARCHITECTURE.md > 'Multi-valued slots'"
        )
    return multi[0] if multi else None


def expand(template: Template, bindings: dict) -> Graph:
    """Expand the template with the given slot bindings. Returns a new Graph
    whose triples are the substituted lowered body."""
    _validate_bindings(template, bindings)
    coerced = _coerce_all(template, bindings) if template.is_instance_form else {}
    multi = _multi_slot(template) if template.is_instance_form else None

    slot_names = {s.name for s in template.slots}

    # Hash key uses string form of all bindings (multi values flattened) so
    # re-expansion of the same instance produces the same intermediate URIs.
    hash_key = (
        {k: [str(v) for v in vs] for k, vs in coerced.items()}
        if template.is_instance_form
        else {k: str(v) for k, v in bindings.items()}
    )
    inst_ns = _instance_ns(template.slug, _binding_hash(template.slug, hash_key))

    # Catalogue intermediate terms in the lowered graph.
    intermediates: set[URIRef] = set()
    for s, _, o in template.lowered:
        for term in (s, o):
            if _is_intermediate(term, template, slot_names):
                intermediates.add(term)

    # Decide which intermediates need per-iteration minting.
    multi_var: URIRef | None = (
        template.var_ns[multi.name] if multi else None
    )
    per_iter = (
        _per_iter_terms(template, multi_var, intermediates)
        if multi_var is not None
        else set()
    )

    out = Graph()

    if template.is_instance_form:
        single_vals = {
            name: (vs[0] if vs else None) for name, vs in coerced.items()
        }

        if multi is None:
            for s, p, o in template.lowered:
                out.add(
                    (
                        _substitute(s, template, single_vals, inst_ns,
                                    per_iter, iter_idx=None),
                        _substitute(p, template, single_vals, inst_ns,
                                    per_iter, iter_idx=None),
                        _substitute(o, template, single_vals, inst_ns,
                                    per_iter, iter_idx=None),
                    )
                )
        else:
            # Triples that don't touch any per-iter term are emitted once with
            # stable URIs; everything else is iterated.
            for s, p, o in template.lowered:
                touches = any(t in per_iter or t == multi_var
                              for t in (s, p, o))
                if not touches:
                    iter_vals = dict(single_vals)
                    iter_vals[multi.name] = (
                        coerced[multi.name][0] if coerced[multi.name] else None
                    )
                    out.add(
                        (
                            _substitute(s, template, iter_vals, inst_ns,
                                        per_iter, iter_idx=None),
                            _substitute(p, template, iter_vals, inst_ns,
                                        per_iter, iter_idx=None),
                            _substitute(o, template, iter_vals, inst_ns,
                                        per_iter, iter_idx=None),
                        )
                    )
            for idx, multi_val in enumerate(coerced[multi.name]):
                iter_vals = dict(single_vals)
                iter_vals[multi.name] = multi_val
                for s, p, o in template.lowered:
                    touches = any(t in per_iter or t == multi_var
                                  for t in (s, p, o))
                    if not touches:
                        continue
                    out.add(
                        (
                            _substitute(s, template, iter_vals, inst_ns,
                                        per_iter, iter_idx=idx),
                            _substitute(p, template, iter_vals, inst_ns,
                                        per_iter, iter_idx=idx),
                            _substitute(o, template, iter_vals, inst_ns,
                                        per_iter, iter_idx=idx),
                        )
                    )
    else:
        # Pattern-form: bindings keyed by variable local-name (any term whose
        # URI starts with var_ns).
        for s, p, o in template.lowered:
            out.add(
                (
                    _substitute_pattern(s, template, bindings, inst_ns),
                    _substitute_pattern(p, template, bindings, inst_ns),
                    _substitute_pattern(o, template, bindings, inst_ns),
                )
            )

    return out


def _substitute(
    term,
    template: Template,
    slot_vals: dict,
    inst_ns: Namespace,
    per_iter: set[URIRef],
    iter_idx: int | None,
):
    """Instance-form substitution. `slot_vals` maps slot_name → coerced single
    value (for multi-valued slot, the value of the current iteration)."""
    if not isinstance(term, URIRef):
        return term
    s = str(term)
    if s.startswith(str(template.var_ns)):
        local = s[len(str(template.var_ns)) :]
        if local in slot_vals and slot_vals[local] is not None:
            return slot_vals[local]
        # intermediate variable
        suffix = (
            f"{local}-{iter_idx}"
            if iter_idx is not None and term in per_iter
            else local
        )
        return URIRef(f"{inst_ns}{suffix}")
    if s.startswith(str(template.anon_ns)):
        local = s[len(str(template.anon_ns)) :]
        suffix = (
            f"anon-{local}-{iter_idx}"
            if iter_idx is not None and term in per_iter
            else f"anon-{local}"
        )
        return URIRef(f"{inst_ns}{suffix}")
    return term


def _substitute_pattern(
    term, template: Template, bindings: dict, inst_ns: Namespace
):
    if not isinstance(term, URIRef):
        return term
    s = str(term)
    if s.startswith(str(template.var_ns)):
        local = s[len(str(template.var_ns)) :]
        if local in bindings:
            v = bindings[local]
            if isinstance(v, (URIRef, Literal)):
                return v
            return URIRef(str(v))
        return URIRef(f"{inst_ns}{local}")
    if s.startswith(str(template.anon_ns)):
        local = s[len(str(template.anon_ns)) :]
        return URIRef(f"{inst_ns}anon-{local}")
    return term
