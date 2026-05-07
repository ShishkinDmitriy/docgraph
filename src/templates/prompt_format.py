"""Render templates into the markdown block that gets injected into LLM
prompts.

Each template renders as:

    ### iso:ClassificationOfIndividual
    Label: Classification of individual
    Definition: [hasClassified] is classified as [hasClassifier] (effective [valEffectiveDate]).
    Variables (lifted form):
      - hasClassified : iso15926:PossibleIndividual
      - hasClassifier : iso15926:ClassOfIndividual
      - valEffectiveDate : (literal — date/time)
    Example:
      ```turtle
      ex:classification-21-P-101-as-centrifugal-pump
          a iso:ClassificationOfIndividual ;
          iso:hasClassified    ex:21-P-101 ;
          iso:hasClassifier    rdl:RDS416834 ;
          iso:valEffectiveDate "2021-07-18T13:59:00Z"^^xsd:dateTime .
      ```

The block is concise enough to fit in prompts even when several templates
apply, but carries enough detail (variables + types + example) for the LLM
to fill it without seeing the lowered body.
"""

from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import NamespaceManager

from src.templates.loader import Template


def _curie_or_uri(term, nm: NamespaceManager) -> str:
    if isinstance(term, URIRef):
        return term.n3(nm)
    if isinstance(term, Literal):
        return term.n3(nm)
    return str(term)


def _build_nm(template: Template) -> NamespaceManager:
    nm = NamespaceManager(Graph(), bind_namespaces="none")
    for prefix, ns in template.prefixes.items():
        nm.bind(prefix, ns, override=True, replace=True)
    return nm


def _lifted_variables(template: Template) -> list[tuple[str, str | None]]:
    """For pattern-form templates, walk the lifted graph collecting
    `(var-local-name, type-curie-or-None)` pairs.

    The "type" is whatever class the lifted graph asserts via `rdf:type` on
    that variable (acts as a soft range hint for the LLM); literals don't
    get one.
    """
    var_prefix = str(template.var_ns)
    nm = _build_nm(template)
    locals_seen: dict[str, str | None] = {}
    type_per_var: dict[str, str] = {}

    from rdflib import RDF
    for s, p, o in template.lifted:
        # Track every variable that appears as subject or object.
        for term in (s, o):
            if isinstance(term, URIRef) and str(term).startswith(var_prefix):
                local = str(term)[len(var_prefix):]
                if local == "this":
                    continue
                locals_seen.setdefault(local, None)
        # Capture type assertions from the lifted body.
        if (
            p == RDF.type
            and isinstance(s, URIRef)
            and str(s).startswith(var_prefix)
            and isinstance(o, URIRef)
        ):
            local = str(s)[len(var_prefix):]
            type_per_var[local] = o.n3(nm)

    # Also capture types from the lowered graph — pattern-form templates
    # often assert variable types only there (e.g., `var:hasClassified a
    # iso15926:PossibleIndividual` lives in the lowered body).
    for s, p, o in template.lowered:
        if (
            p == RDF.type
            and isinstance(s, URIRef)
            and str(s).startswith(var_prefix)
            and isinstance(o, URIRef)
        ):
            local = str(s)[len(var_prefix):]
            if local in locals_seen:
                type_per_var.setdefault(local, o.n3(nm))

    return [(local, type_per_var.get(local)) for local in sorted(locals_seen)]


def _instance_form_variables(template: Template) -> list[tuple[str, str | None]]:
    nm = _build_nm(template)
    out: list[tuple[str, str | None]] = []
    for slot in template.slots:
        rng = slot.range.n3(nm) if slot.range is not None else None
        out.append((slot.name, rng))
    return out


def _serialize_example(template: Template) -> str | None:
    if template.example is None or not list(template.example):
        return None
    nm = _build_nm(template)
    # Re-bind common namespaces explicitly so the serializer emits CURIEs
    # rather than full URIs even if a prefix wasn't carried into
    # `template.prefixes`.
    g = Graph()
    for prefix, ns in template.prefixes.items():
        g.bind(prefix, ns, override=True, replace=True)
    for triple in template.example:
        g.add(triple)
    return g.serialize(format="turtle").strip()


def render_template(template: Template) -> str:
    """Format one template as a markdown spec block."""
    lines: list[str] = []
    nm = _build_nm(template)

    lines.append(f"### {template.uri.n3(nm)}")
    if template.label:
        lines.append(f"Label: {template.label}")
    if template.subject is not None:
        lines.append(f"Subject: {template.subject.n3(nm)}")
    if template.definition:
        lines.append(f"Definition: {template.definition}")

    if template.is_instance_form:
        vars_list = _instance_form_variables(template)
    else:
        vars_list = _lifted_variables(template)

    if vars_list:
        lines.append("Variables:")
        for name, rng in vars_list:
            if rng:
                lines.append(f"  - {name} : {rng}")
            else:
                lines.append(f"  - {name}")
            desc = template.var_descriptions.get(name)
            if desc:
                # Indent every line of a multi-line description so the
                # prompt's bullet structure stays intact.
                for line in desc.splitlines():
                    lines.append(f"      {line}")

    example = _serialize_example(template)
    if example:
        lines.append("Example:")
        lines.append("```turtle")
        lines.append(example)
        lines.append("```")

    return "\n".join(lines)


def render_templates(templates: list[Template]) -> str:
    """Render a list of templates as a single markdown section.

    Returns "(no templates apply)" when the list is empty so the prompt
    body still has a sensible value at the placeholder site.
    """
    if not templates:
        return "(no templates apply for this prompt)"
    blocks = [render_template(t) for t in templates]
    return "\n\n".join(blocks)
