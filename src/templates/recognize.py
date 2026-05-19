"""Recognize template instances in a graph (the lowered → lifted direction).

Translates the template's lowered body to a SPARQL SELECT query, runs it
against the input graph, and post-processes the result rows into per-instance
slot-binding dicts. For multi-valued slots, multiple rows that share the same
non-multi bindings are folded into a single instance whose multi-slot value is
a list.

Used at display time (inspector folds reified clusters back to template form)
and at ingest time when foreign Part 2 data wasn't authored as templates but
still matches known patterns.

The translation is purely structural — no per-template SPARQL is stored on
disk. Templates remain declarative; SPARQL is an execution detail.
"""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import NamespaceManager

from src.templates.loader import Template


def _is_var(term, template: Template) -> bool:
    if not isinstance(term, URIRef):
        return False
    s = str(term)
    return s.startswith(str(template.var_ns)) or s.startswith(
        str(template.anon_ns)
    )


def _sparql_var_name(term: URIRef, template: Template) -> str:
    """SPARQL variable name for a lowered-graph variable URI."""
    s = str(term)
    if s.startswith(str(template.var_ns)):
        return s[len(str(template.var_ns)) :]
    if s.startswith(str(template.anon_ns)):
        # Anon localnames like "_b0" → SPARQL var "anon_b0" (dedicated prefix
        # so they can never collide with slot names).
        return "anon" + s[len(str(template.anon_ns)) :]
    raise ValueError(f"term {term!r} is not in template's var/anon namespace")


def _build_namespace_manager(template: Template) -> NamespaceManager:
    """Build a NamespaceManager seeded with the template's captured prefixes
    so URIs serialize as CURIEs where possible."""
    nm = NamespaceManager(Graph(), bind_namespaces="none")
    for prefix, ns in template.prefixes.items():
        nm.bind(prefix, ns, override=True, replace=True)
    return nm


def _serialize_term(term, template: Template, nm: NamespaceManager,
                    used_prefixes: set[str]) -> str:
    if _is_var(term, template):
        return f"?{_sparql_var_name(term, template)}"
    if isinstance(term, URIRef):
        n3 = term.n3(nm)
        # n3() returns either `<full-uri>` or `prefix:local`; track which
        # prefixes were used so we only emit declarations for the live ones.
        if not n3.startswith("<"):
            prefix = n3.split(":", 1)[0]
            used_prefixes.add(prefix)
        return n3
    if isinstance(term, Literal):
        return term.n3(nm)
    raise ValueError(f"unexpected term: {term!r}")


def to_sparql(template: Template) -> str:
    """Translate `template.lowered` to a `SELECT * WHERE { ... }` query.

    Slot variables and named intermediates use SPARQL var names matching their
    URI local-names; anon URIs (former blank nodes) get an `anon` prefix on
    their local-name to avoid colliding with slot names. Concrete URIs use the
    template's captured prefix declarations to emit CURIE-shaped terms.
    Triples are emitted in (s, p, o)-sorted order so the generated query is
    deterministic across runs (handy for review and for golden-file tests).
    """
    nm = _build_namespace_manager(template)
    used_prefixes: set[str] = set()

    bgp_lines = []
    for s, p, o in sorted(
        template.lowered, key=lambda t: (str(t[0]), str(t[1]), str(t[2]))
    ):
        bgp_lines.append(
            f"  {_serialize_term(s, template, nm, used_prefixes)} "
            f"{_serialize_term(p, template, nm, used_prefixes)} "
            f"{_serialize_term(o, template, nm, used_prefixes)} ."
        )

    prefix_lines = [
        f"PREFIX {prefix}: <{template.prefixes[prefix]}>"
        for prefix in sorted(used_prefixes)
        if prefix in template.prefixes
    ]

    body = "\n".join(bgp_lines)
    if prefix_lines:
        return "\n".join(prefix_lines) + f"\n\nSELECT * WHERE {{\n{body}\n}}"
    return f"SELECT * WHERE {{\n{body}\n}}"


def _pattern_form_var_locals(template: Template) -> set[str]:
    """Variable local-names visible in a pattern-form template's lifted graph."""
    locals_: set[str] = set()
    var_prefix = str(template.var_ns)
    for s, p, o in template.lifted:
        for term in (s, p, o):
            if isinstance(term, URIRef) and str(term).startswith(var_prefix):
                locals_.add(str(term)[len(var_prefix) :])
    return locals_


def recognize(template: Template, graph: Graph) -> list[dict]:
    """Find every match of `template`'s lowered body in `graph`.

    Returns a list of binding dicts:

    - **Instance-form templates**: dict keys are slot names; values are RDF
      terms (URIRef / Literal). Multi-valued slots collect a list of values
      from every result row sharing the same non-multi bindings.
    - **Pattern-form templates**: dict keys are the variable local-names from
      the lifted graph (e.g. `"entity"`, `"activity"` for the PROV-O bridge).

    Returns `[]` when nothing matches. Returns one dict per recognized instance.
    """
    sparql = to_sparql(template)
    rows = list(graph.query(sparql))

    if not template.is_instance_form:
        var_locals = _pattern_form_var_locals(template)
        return [
            {local: row[local] for local in var_locals if local in row.asdict()}
            for row in rows
        ]

    slot_names = [s.name for s in template.slots]
    multi = next((s for s in template.slots if s.is_multi), None)

    if multi is None:
        return [
            {name: row[name] for name in slot_names if name in row.asdict()}
            for row in rows
        ]

    # Multi-valued: group by non-multi bindings, collect multi values.
    non_multi = [n for n in slot_names if n != multi.name]
    grouped: dict[tuple, dict] = {}
    for row in rows:
        row_dict = row.asdict()
        key = tuple(row_dict.get(n) for n in non_multi)
        if key not in grouped:
            inst = {n: row_dict.get(n) for n in non_multi}
            inst[multi.name] = []
            grouped[key] = inst
        if multi.name in row_dict:
            grouped[key][multi.name].append(row_dict[multi.name])
    return list(grouped.values())
