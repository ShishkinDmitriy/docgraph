"""Load a template TTL/TriG file into an in-memory Template record.

Template files use a `var:` CURIE prefix mapped to `urn:tpl-var/` for all
variables (slot, intermediate, and graph URIs). Example::

    @prefix var: <urn:tpl-var/> .

    dg:SourcedAssertion a tpl:Template ;
        tpl:slot var:doc, var:quoteText, var:locator, var:references ;
        tpl:lowered var:lowered .

    var:doc tpl:range dg:Document .
    ...

    GRAPH var:lowered {
        var:quote a dg:Quote ; dg:text var:quoteText ; ... .
        ...
    }

At load time the loader **skolemizes** every URI in `urn:tpl-var/` to a per-
template namespace `urn:tpl/<slug>/var/`, where `<slug>` is the kebab-case
local-name of the template URI. This avoids cross-template aliasing if multiple
templates ever live in the same in-memory dataset.

Variable roles (used by the expansion engine):

- **Slot variable** — listed under `tpl:slot`. The slot's name is the URI's
  local-name; metadata (`tpl:range`, `tpl:minCount`, `tpl:maxCount`) attaches
  via that URI as subject.
- **Intermediate variable** — appears in the lowered graph but is not listed
  under `tpl:slot`. Treated as identity-stable (one per template instance,
  shared across iterations of a multi-valued slot).
- **Anon URI** — what the loader assigns to former blank nodes (`[ ... ]`) in
  lifted/lowered. Lives in `urn:tpl/<slug>/anon/`. Per-iteration when reachable
  from the multi-valued slot; otherwise stable.

For instance-form templates the lifted graph is auto-derived from the slot
list at load time:

    var:this a <template-uri> .
    var:this <urn:tpl/<slug>/slot/<name>> var:<name> .   (per slot)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from rdflib import BNode, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.graph import DATASET_DEFAULT_GRAPH_ID
from rdflib.namespace import RDF, RDFS, XSD

TPL = Namespace("http://example.org/docgraph/template#")
VAR_SOURCE_PREFIX = "urn:tpl-var/"


@dataclass
class Slot:
    name: str
    range: URIRef | None = None
    min_count: int = 1
    max_count: int = 1  # 0 means unbounded
    is_literal: bool = False

    @property
    def is_multi(self) -> bool:
        return self.max_count == 0 or self.max_count > 1


@dataclass
class Template:
    uri: URIRef
    slug: str
    var_ns: Namespace
    anon_ns: Namespace
    lifted: Graph
    lowered: Graph
    is_instance_form: bool = True
    label: str | None = None
    definition: str | None = None
    subject: URIRef | None = None
    slots: list[Slot] = field(default_factory=list)
    # `tpl:example` graph (when declared) — documentation-only, not consumed
    # by expansion or recognition. Useful for LLM prompts that show worked
    # examples of the lifted form.
    example: Graph | None = None
    # Per-variable natural-language descriptions, captured from
    # `<urn:tpl-var/X> rdfs:comment "..."` triples in the meta graph. Keyed
    # by variable local-name (e.g. "hasPossessor"). Surfaced by the prompt
    # renderer so the LLM sees what each role *means*, beyond its Part 2
    # type. Generic mechanism — applies equally to instance-form slots and
    # pattern-form variables.
    var_descriptions: dict[str, str] = field(default_factory=dict)
    # Per-variable datatype URIs (xsd:decimal / xsd:dateTime / ...), inferred
    # from the lowered graph's structural typing of literal-bearing variables.
    # See `_infer_var_datatypes` for the recognised patterns. Keyed by
    # variable local-name. The materialiser uses this to type literal-valued
    # bindings instead of emitting plain string literals.
    var_datatypes: dict[str, URIRef] = field(default_factory=dict)
    # `@prefix` declarations from the source file, captured for downstream
    # use (e.g., emitting CURIE-shaped SPARQL). The `var:` prefix is dropped
    # since its URIs are skolemized to per-template namespaces.
    prefixes: dict[str, str] = field(default_factory=dict)

    def slot(self, name: str) -> Slot | None:
        for s in self.slots:
            if s.name == name:
                return s
        return None


ISO15926 = Namespace("http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#")

# One-to-one mapping from Part 2 representation/numeric classes to the xsd
# datatype the corresponding literal binding takes. The lowered graph types
# the *intermediate* node with one of these classes; the literal-bound slot
# variable is reachable from that node via a Part 2 idiom (see
# `_LITERAL_VAR_FROM_TYPED_NODE` below).
#
# Extend this map as more Part 2 representation classes appear in templates.
_DATATYPE_BY_PART2_CLASS: dict[URIRef, URIRef] = {
    ISO15926.RepresentationOfGregorianDateAndUtcTime: XSD.dateTime,
    ISO15926.RealNumber:       XSD.decimal,
    ISO15926.ArithmeticNumber: XSD.decimal,
    ISO15926.IntegerNumber:    XSD.integer,
}


def _literal_var_locals_for_typed_node(
    lowered: Graph, typed_node, var_prefix: str
) -> list[str]:
    """Given a Part 2-typed intermediate node, return the local-names of any
    slot variables it stamps a datatype onto.

    Part 2 uses two idioms to connect a representation/number node to its
    literal binding:

    1. **Direct `hasContent`** — used by `RepresentationOfGregorianDateAndUtcTime`
       and similar classes that *are* the representation.
       Pattern: ``?typed iso15926:hasContent ?var``.

    2. **Indirect via `Identification`** — used by `RealNumber`,
       `ArithmeticNumber`, `IntegerNumber` etc. The number is the
       *represented thing*; the literal sign reaches it through an
       Identification reification.
       Pattern: ``?id a iso15926:Identification ;
                       iso15926:hasRepresented ?typed ;
                       iso15926:hasSign        ?var .``

    Both idioms come straight from Part 2's reification rules (templates.md
    documents them). The mapping table itself stays one-to-one — we only
    need two graph-walk shapes to *find* which variable a given typed node
    stamps.
    """
    out: list[str] = []

    def _local_if_var(term):
        if isinstance(term, URIRef) and str(term).startswith(var_prefix):
            return str(term)[len(var_prefix):]
        return None

    # Idiom 1: direct hasContent.
    for _, _, content in lowered.triples(
        (typed_node, ISO15926.hasContent, None)
    ):
        local = _local_if_var(content)
        if local:
            out.append(local)

    # Idiom 2: Identification with hasRepresented = typed_node and
    # hasSign = ?var.
    for id_node, _, _ in lowered.triples(
        (None, ISO15926.hasRepresented, typed_node)
    ):
        # Confirm the id_node is actually an Identification.
        if (id_node, RDF.type, ISO15926.Identification) not in lowered:
            continue
        for _, _, sign in lowered.triples(
            (id_node, ISO15926.hasSign, None)
        ):
            local = _local_if_var(sign)
            if local:
                out.append(local)

    return out


def _infer_var_datatypes(
    lowered: Graph, var_ns: Namespace
) -> dict[str, URIRef]:
    """Walk the lowered graph and return a per-variable xsd datatype map.

    Conceptually a single one-to-one mapping `(Part 2 class) → (xsd type)`:
    if a variable plays the literal role for a Part 2 representation/number
    class, its xsd type is determined. The two graph-walk idioms in
    `_literal_var_locals_for_typed_node` are just two ways the lowered
    graph can connect the typed node to its literal slot.
    """
    out: dict[str, URIRef] = {}
    var_prefix = str(var_ns)
    for typed_node, _, type_class in lowered.triples((None, RDF.type, None)):
        dt = _DATATYPE_BY_PART2_CLASS.get(type_class)
        if dt is None:
            continue
        for local in _literal_var_locals_for_typed_node(
            lowered, typed_node, var_prefix
        ):
            out.setdefault(local, dt)
    return out


def _is_literal_range(rng: URIRef | None) -> bool:
    if rng is None:
        return False
    s = str(rng)
    return s.startswith(str(XSD)) or s == str(RDFS.Literal)


def slot_predicate(template_slug: str, slot_name: str) -> URIRef:
    """Synthesise the slot's lifted-form predicate URI.

    Per-template namespace `urn:tpl/<slug>/slot/<slot-name>` — independent of
    the template URI's shape (which may already contain a `#`).
    """
    return URIRef(f"urn:tpl/{template_slug}/slot/{slot_name}")


_PASCAL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def slug_from_template_uri(uri: URIRef) -> str:
    """Derive a kebab-case slug from a template URI's local-name.

    Examples:
        dg:SourcedAssertion        -> sourced-assertion
        dom:InvoiceHasVatNumber    -> invoice-has-vat-number
        <urn:tpl/prov-wgb>         -> prov-wgb
    """
    s = str(uri)
    if "#" in s:
        local = s.rsplit("#", 1)[1]
    else:
        local = s.rsplit("/", 1)[1]
    if not local:
        raise ValueError(f"cannot derive slug from {uri!r}: empty local-name")
    return _PASCAL_BOUNDARY.sub("-", local).lower()


def _localname(uri: URIRef) -> str:
    """Return the local-name of a URI (after the last `#` or `/`)."""
    s = str(uri)
    if "#" in s:
        return s.rsplit("#", 1)[1]
    return s.rsplit("/", 1)[1]


def _expand_invocations(
    g: Graph,
    registry: dict[URIRef, "Template"],
    outer_anon_ns: Namespace,
    counter: list[int],
) -> Graph:
    """Inline every invocation of a registered template into `g`.

    An invocation is any node `?inv` carrying a triple `?inv rdf:type T` where
    `T` is a URI registered in `registry`. The other triples on `?inv` whose
    predicate local-name matches a slot of `T` are slot bindings.

    Inlining replaces:
    - `T`'s `var:this` with `?inv` (so the OUTER's containing triples still
      refer to the same node);
    - `T`'s slot variables with the bound values;
    - `T`'s intermediate variables and anon URIs with fresh URIs in
      `outer_anon_ns` (per-invocation map, so an inner intermediate that
      appears in N triples gets the same outer URI in all N).

    The invocation's own `rdf:type T` and slot-binding triples are dropped from
    the result. Recursively re-runs until no more invocations exist (so inner
    templates may themselves invoke other templates).
    """
    if not registry:
        return g

    # Find invocation nodes: (subject, invoked-template).
    invocations: list[tuple, ] = []
    seen_invocation_subjects: set = set()
    for s, _, o in g.triples((None, RDF.type, None)):
        if isinstance(o, URIRef) and o in registry and s not in seen_invocation_subjects:
            invocations.append((s, registry[o]))
            seen_invocation_subjects.add(s)

    if not invocations:
        return g

    # Triples to drop from the outer (the rdf:type marker + slot-binding
    # triples that have predicate-localname == slot name of the invoked
    # template).
    to_drop: set = set()
    bindings_per_invocation: dict = {}

    for inv_node, invoked in invocations:
        slot_names = {sl.name for sl in invoked.slots}
        bindings: dict = {}
        for s, p, o in g.triples((inv_node, None, None)):
            if p == RDF.type and o == invoked.uri:
                to_drop.add((s, p, o))
                continue
            local = _localname(p)
            if local in slot_names:
                bindings[local] = o
                to_drop.add((s, p, o))
        bindings_per_invocation[inv_node] = bindings

    out = Graph()
    for triple in g:
        if triple not in to_drop:
            out.add(triple)

    # Inline each invocation's lowered body.
    for inv_node, invoked in invocations:
        bindings = bindings_per_invocation[inv_node]
        per_inv_remap: dict = {}

        def _sub(term):
            if not isinstance(term, URIRef):
                return term
            t = str(term)
            inner_var = str(invoked.var_ns)
            inner_anon = str(invoked.anon_ns)

            # Inner's anchor `var:this` → the invocation node itself.
            if t == inner_var + "this":
                return inv_node

            # Slot variable → binding (if provided).
            if t.startswith(inner_var):
                local = t[len(inner_var):]
                if local in bindings:
                    return bindings[local]
                # Intermediate (not a slot, not `this`): mint per-invocation
                # fresh URI in the OUTER's anon namespace.
                if term not in per_inv_remap:
                    per_inv_remap[term] = outer_anon_ns[
                        f"_b{counter[0]}"
                    ]
                    counter[0] += 1
                return per_inv_remap[term]

            # Inner anon URI → fresh outer anon URI (per-invocation).
            if t.startswith(inner_anon):
                if term not in per_inv_remap:
                    per_inv_remap[term] = outer_anon_ns[
                        f"_b{counter[0]}"
                    ]
                    counter[0] += 1
                return per_inv_remap[term]

            return term

        for s, p, o in invoked.lowered:
            out.add((_sub(s), _sub(p), _sub(o)))

    # Recurse: an inlined inner body could itself contain invocations of yet
    # other registered templates.
    return _expand_invocations(out, registry, outer_anon_ns, counter)


def _build_lifted(
    tpl_uri: URIRef, slug: str, slots: list[Slot], var_ns: Namespace
) -> Graph:
    """Auto-derive the lifted graph for an instance-form template."""
    g = Graph()
    var_this = var_ns["this"]
    g.add((var_this, RDF.type, tpl_uri))
    for slot in slots:
        g.add((var_this, slot_predicate(slug, slot.name), var_ns[slot.name]))
    return g


def _read_slots(
    meta: Graph, tpl_uri: URIRef, var_ns: Namespace, skol
) -> list[Slot]:
    """Read slots from the metadata graph. Each `?t tpl:slot ?slotvar` triple
    declares a slot whose URI is the variable. Slot name = URI local-name.
    Slot metadata (`tpl:range`, etc.) is attached via the same URI as subject.
    """
    slots: list[Slot] = []
    for slot_var_raw in meta.objects(tpl_uri, TPL.slot):
        if not isinstance(slot_var_raw, URIRef):
            raise ValueError(
                f"tpl:slot value {slot_var_raw!r} must be a URI in the var: "
                f"namespace"
            )
        slot_var = skol(slot_var_raw)
        if not str(slot_var).startswith(str(var_ns)):
            raise ValueError(
                f"tpl:slot value {slot_var_raw!r} is not in the var: "
                f"namespace ({VAR_SOURCE_PREFIX!r})"
            )
        name = str(slot_var)[len(str(var_ns)) :]
        if not name:
            raise ValueError(
                f"tpl:slot value {slot_var_raw!r} has empty local-name"
            )
        rng = meta.value(slot_var_raw, TPL.range)
        if rng is not None and not isinstance(rng, URIRef):
            raise ValueError(
                f"tpl:range of slot {name!r} must be a URI, got {rng!r}"
            )
        min_lit = meta.value(slot_var_raw, TPL.minCount)
        max_lit = meta.value(slot_var_raw, TPL.maxCount)
        slots.append(
            Slot(
                name=name,
                range=rng if isinstance(rng, URIRef) else None,
                min_count=int(min_lit) if min_lit is not None else 1,
                max_count=int(max_lit) if max_lit is not None else 1,
                is_literal=_is_literal_range(
                    rng if isinstance(rng, URIRef) else None
                ),
            )
        )
    slots.sort(key=lambda s: s.name)
    return slots


def load_template(
    path: str | Path,
    registry: dict[URIRef, "Template"] | None = None,
) -> Template:
    """Parse one template file and return a Template record.

    `registry` is an optional `{template_uri: Template}` map. If a template's
    lowered (or pattern-form lifted) body contains an invocation — a node typed
    as a registered template — the loader inlines that template's lowered body
    in place of the invocation. The invocation node `?inv` plays the role of
    the inner template's `var:this`; binding triples on `?inv` whose predicate
    local-name matches a slot of the invoked template supply the slot values.
    Inner intermediates and anon URIs are re-minted into this template's anon
    namespace so the outer body is namespace-self-contained.
    """
    registry = registry or {}
    path = Path(path)
    ds = Dataset(default_union=False)
    ds.parse(str(path), format="trig")

    meta = ds.graph(DATASET_DEFAULT_GRAPH_ID)

    tpl_uris = list(meta.subjects(RDF.type, TPL.Template))
    if not tpl_uris:
        raise ValueError(f"no tpl:Template found in {path}")
    if len(tpl_uris) > 1:
        raise ValueError(f"multiple tpl:Templates in {path}; one per file")
    tpl_uri = tpl_uris[0]
    if not isinstance(tpl_uri, URIRef):
        raise ValueError(f"template subject {tpl_uri!r} must be a URI")

    slug = slug_from_template_uri(tpl_uri)
    var_ns = Namespace(f"urn:tpl/{slug}/var/")
    anon_ns = Namespace(f"urn:tpl/{slug}/anon/")

    def skol(term):
        """Skolemize: any URI in the source's var: namespace becomes a URI in
        this template's var: namespace."""
        if isinstance(term, URIRef) and str(term).startswith(VAR_SOURCE_PREFIX):
            return URIRef(
                str(var_ns) + str(term)[len(VAR_SOURCE_PREFIX) :]
            )
        return term

    # Anchor URIs for the lifted/lowered named graphs (still in source form).
    lowered_raw_uri = meta.value(tpl_uri, TPL.lowered)
    if not isinstance(lowered_raw_uri, URIRef):
        raise ValueError(f"template {tpl_uri} has no tpl:lowered URI")
    lifted_raw_uri = meta.value(tpl_uri, TPL.lifted)
    if lifted_raw_uri is not None and not isinstance(lifted_raw_uri, URIRef):
        raise ValueError(
            f"tpl:lifted of {tpl_uri} must be a URI, got {lifted_raw_uri!r}"
        )

    # Shared anon counter across skolemize + invocation expansion + bnode
    # replacement, so every minted anon URI in this template's lowered/lifted
    # gets a unique `_b<N>` localname regardless of origin.
    anon_counter = [0]

    def _skolemize_graph(named_graph: Graph) -> Graph:
        """Step 1 — only URI rewriting; bnodes pass through unchanged."""
        out = Graph()
        for s, p, o in sorted(
            named_graph, key=lambda t: (str(t[0]), str(t[1]), str(t[2]))
        ):
            out.add((skol(s), skol(p), skol(o)))
        return out

    bnode_map: dict[BNode, URIRef] = {}

    def _replace_bnodes_inplace(g: Graph) -> Graph:
        """Step 3 — replace any remaining bnodes with deterministic anon URIs.
        Sorted iteration so id assignment is stable across re-parses."""
        out = Graph()

        def remap(t):
            if isinstance(t, BNode):
                if t not in bnode_map:
                    bnode_map[t] = anon_ns[f"_b{anon_counter[0]}"]
                    anon_counter[0] += 1
                return bnode_map[t]
            return t

        for s, p, o in sorted(
            g, key=lambda t: (str(t[0]), str(t[1]), str(t[2]))
        ):
            out.add((remap(s), remap(p), remap(o)))
        return out

    raw_lowered = ds.graph(lowered_raw_uri)
    if not list(raw_lowered):
        raise ValueError(
            f"tpl:lowered graph {lowered_raw_uri} is empty in {path}"
        )
    lowered_skolemized = _skolemize_graph(raw_lowered)
    lowered_expanded = _expand_invocations(
        lowered_skolemized, registry, anon_ns, anon_counter
    )
    lowered = _replace_bnodes_inplace(lowered_expanded)

    slots = _read_slots(meta, tpl_uri, var_ns, skol)

    if lifted_raw_uri is not None:
        if slots:
            raise ValueError(
                f"pattern-form template {tpl_uri} declares both tpl:lifted "
                f"and tpl:slot — one or the other"
            )
        raw_lifted = ds.graph(lifted_raw_uri)
        if not list(raw_lifted):
            raise ValueError(
                f"tpl:lifted graph {lifted_raw_uri} is empty in {path}"
            )
        lifted_skolemized = _skolemize_graph(raw_lifted)
        lifted_expanded = _expand_invocations(
            lifted_skolemized, registry, anon_ns, anon_counter
        )
        lifted = _replace_bnodes_inplace(lifted_expanded)
        is_instance_form = False
    else:
        if not slots:
            raise ValueError(
                f"template {tpl_uri} has no tpl:slot and no tpl:lifted"
            )
        lifted = _build_lifted(tpl_uri, slug, slots, var_ns)
        is_instance_form = True

    label = meta.value(tpl_uri, RDFS.label)
    definition = meta.value(tpl_uri, TPL.definition)
    subject = meta.value(tpl_uri, TPL.subject)

    # Per-variable descriptions: any `<urn:tpl-var/X> rdfs:comment "..."`
    # triple in the meta graph. Keyed by variable local-name post-skolem.
    var_descriptions: dict[str, str] = {}
    for s, _, o in meta.triples((None, RDFS.comment, None)):
        if isinstance(s, URIRef) and str(s).startswith(VAR_SOURCE_PREFIX):
            local = str(s)[len(VAR_SOURCE_PREFIX):]
            if local and isinstance(o, Literal):
                var_descriptions[local] = str(o)

    var_datatypes = _infer_var_datatypes(lowered, var_ns)

    example: Graph | None = None
    example_raw_uri = meta.value(tpl_uri, TPL.example)
    if isinstance(example_raw_uri, URIRef):
        raw_example = ds.graph(example_raw_uri)
        if list(raw_example):
            # Example is documentation; serialize URIs as-is (no skolemization
            # of var: namespace, since example bodies use concrete URIs anyway).
            example = Graph()
            for s, p, o in raw_example:
                example.add((s, p, o))

    prefixes: dict[str, str] = {}
    for prefix, ns in ds.namespaces():
        if not prefix:
            continue
        if str(ns) == VAR_SOURCE_PREFIX:
            # `var:` is handled by skolemization; its URIs never survive into
            # downstream graphs, so the prefix isn't useful afterwards.
            continue
        prefixes[prefix] = str(ns)

    return Template(
        uri=tpl_uri,
        slug=slug,
        var_ns=var_ns,
        anon_ns=anon_ns,
        lifted=lifted,
        lowered=lowered,
        is_instance_form=is_instance_form,
        label=str(label) if isinstance(label, Literal) else None,
        definition=(
            str(definition) if isinstance(definition, Literal) else None
        ),
        subject=subject if isinstance(subject, URIRef) else None,
        slots=slots,
        example=example,
        var_descriptions=var_descriptions,
        var_datatypes=var_datatypes,
        prefixes=prefixes,
    )
