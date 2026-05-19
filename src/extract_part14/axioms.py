"""Ontology-axiom helpers for the part14 walker.

Pure SPARQL/rdflib queries over a single loaded `Graph`. No LLM calls, no
I/O. The walker calls these instead of reimplementing graph traversal —
keeps the walker generic across whichever upper ontology is loaded.

Most calls take a Graph that's a flat union of the project's loaded named
graphs (built via `loader.union_view(dataset)`). SPARQL queries against
rdflib `Dataset` only see the default graph by default, so we pre-flatten.

For LIS-14 specifically, the helpers exploit:
- 1 `owl:AllDisjointClasses` axiom at the top (Activity ⊥ Aspect ⊥ Object)
- ~12 `owl:inverseOf` pairs on properties
- many `rdfs:subPropertyOf` chains
- `rdfs:domain` / `rdfs:range` on most properties
- `rdfs:label` everywhere; `rdfs:comment` / `skos:definition` on most
"""

from __future__ import annotations

import functools
from collections.abc import Iterable

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS


def effective_branches(
    dataset: Graph,
    namespace: str | None = None,
    descent_threshold: int = 3,
    max_depth: int = 4,
) -> list[URIRef]:
    """Compute the branches the walker actually visits.

    Starts from `top_level_classes` (filtered by *namespace* — typically the
    upper ontology's namespace, so PROV-O / OA / dg:plumbing classes don't
    appear as top-level branches). Then descends **recursively** through
    `rdfs:subClassOf` (across namespaces — domain ontologies layered on top
    of the upper ontology surface here), replacing a branch with its
    extractable direct subclasses while:
      - the branch has at least *descent_threshold* extractable direct
        subclasses, AND
      - we're not yet at *max_depth*.

    Subclasses of any namespace count, but each candidate is filtered through
    `is_extractable` so docgraph-plumbing classes (dg:Document, dg:Quote,
    etc., all marked `dg:extractable false`) are skipped. A user-loaded
    domain ontology that declares `inv:Invoice rdfs:subClassOf
    lis:InformationObject` will surface as a sub-branch automatically.
    """
    visited: set[URIRef] = set()
    out: list[URIRef] = []

    def _descend(cls: URIRef, depth: int) -> None:
        if cls in visited:
            return
        visited.add(cls)
        if depth >= max_depth:
            out.append(cls)
            return
        children = subclasses(dataset, cls, direct=True)
        extractable_children = [c for c in children if is_extractable(dataset, c)]
        if len(extractable_children) >= descent_threshold:
            for child in extractable_children:
                _descend(child, depth + 1)
        else:
            out.append(cls)

    for top in top_level_classes(dataset, namespace=namespace):
        if not is_extractable(dataset, top):
            continue
        _descend(top, depth=0)

    return sorted(out, key=str)


def top_level_classes(dataset: Graph, namespace: str | None = None) -> list[URIRef]:
    """Return classes whose only super-classes are owl:Thing or sit outside
    the loaded ontologies.

    *namespace*: if given (e.g. "http://rds.posccaesar.org/ontology/lis14/rdl/"),
    restricts to classes in that namespace. Otherwise returns all top-level
    classes across the loaded dataset.
    """
    query = """
    PREFIX owl:  <http://www.w3.org/2002/07/owl#>
    PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
    SELECT DISTINCT ?c WHERE {
        ?c a owl:Class .
        FILTER NOT EXISTS {
            ?c rdfs:subClassOf ?super .
            FILTER (?super != owl:Thing && !isBlank(?super))
        }
    }
    """
    classes: list[URIRef] = []
    for row in dataset.query(query):
        c = row[0]
        if not isinstance(c, URIRef):
            continue
        if namespace and not str(c).startswith(namespace):
            continue
        classes.append(c)
    return sorted(classes, key=str)


def subclasses(dataset: Graph, cls: URIRef, *, direct: bool = True) -> list[URIRef]:
    """Return classes declared as `rdfs:subClassOf cls`.

    *direct*=True: only immediate subclasses.
    *direct*=False: transitive — every descendant.
    """
    if direct:
        query = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?c WHERE { ?c rdfs:subClassOf ?cls . FILTER(!isBlank(?c)) }
        """
        bindings = {"cls": cls}
    else:
        query = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?c WHERE { ?c rdfs:subClassOf+ ?cls . FILTER(!isBlank(?c)) }
        """
        bindings = {"cls": cls}
    out = []
    for row in dataset.query(query, initBindings=bindings):
        if isinstance(row[0], URIRef):
            out.append(row[0])
    return sorted(out, key=str)


def superclasses(dataset: Graph, cls: URIRef, *, direct: bool = True) -> list[URIRef]:
    """Inverse of subclasses()."""
    if direct:
        query = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?c WHERE { ?cls rdfs:subClassOf ?c . FILTER(!isBlank(?c)) }
        """
    else:
        query = """
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        SELECT DISTINCT ?c WHERE { ?cls rdfs:subClassOf+ ?c . FILTER(!isBlank(?c)) }
        """
    out = []
    for row in dataset.query(query, initBindings={"cls": cls}):
        if isinstance(row[0], URIRef):
            out.append(row[0])
    return sorted(out, key=str)


def disjoint_with(dataset: Graph, cls: URIRef) -> set[URIRef]:
    """Return all classes disjoint with *cls*, including disjointness inherited
    via subClassOf and via owl:AllDisjointClasses set assertions.

    For LIS-14: passing `lis:Activity` returns `{lis:Aspect, lis:Object}` plus
    every subclass of those (transitively). One axiom does a lot of work.
    """
    # First, find all classes directly disjoint via owl:disjointWith or via
    # AllDisjointClasses memberships.
    direct: set[URIRef] = set()

    # Pairwise owl:disjointWith
    for row in dataset.query(
        """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        SELECT DISTINCT ?other WHERE {
            { ?cls owl:disjointWith ?other } UNION
            { ?other owl:disjointWith ?cls }
            FILTER(!isBlank(?other))
        }
        """,
        initBindings={"cls": cls},
    ):
        if isinstance(row[0], URIRef):
            direct.add(row[0])

    # AllDisjointClasses (owl:members is an RDF list; we need every other
    # member of every set the cls appears in)
    for row in dataset.query(
        """
        PREFIX owl: <http://www.w3.org/2002/07/owl#>
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        SELECT DISTINCT ?other WHERE {
            ?adc a owl:AllDisjointClasses ;
                 owl:members/rdf:rest*/rdf:first ?cls ;
                 owl:members/rdf:rest*/rdf:first ?other .
            FILTER (?other != ?cls && !isBlank(?other))
        }
        """,
        initBindings={"cls": cls},
    ):
        if isinstance(row[0], URIRef):
            direct.add(row[0])

    # Inheritance: also disjoint with cls's super-classes' direct disjoints
    # (Activity ⊥ Object → Activity ⊥ all subclasses of Object)
    for sup in superclasses(dataset, cls, direct=False):
        for row in dataset.query(
            """
            PREFIX owl: <http://www.w3.org/2002/07/owl#>
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            SELECT DISTINCT ?other WHERE {
                {
                  { ?sup owl:disjointWith ?other } UNION
                  { ?other owl:disjointWith ?sup }
                } UNION {
                  ?adc a owl:AllDisjointClasses ;
                       owl:members/rdf:rest*/rdf:first ?sup ;
                       owl:members/rdf:rest*/rdf:first ?other .
                  FILTER (?other != ?sup)
                }
                FILTER(!isBlank(?other))
            }
            """,
            initBindings={"sup": sup},
        ):
            if isinstance(row[0], URIRef):
                direct.add(row[0])

    # Now expand: each disjoint class's subclasses are also disjoint with cls
    # (Activity ⊥ Object → Activity ⊥ Person, since Person ⊆ Object)
    expanded = set(direct)
    for d in direct:
        expanded.update(subclasses(dataset, d, direct=False))
    return expanded


def inverse_of(dataset: Graph, prop: URIRef) -> URIRef | None:
    """Return the property declared as `owl:inverseOf` of *prop*, or None.

    Direct iteration over rdflib indexes — vastly faster than equivalent
    SPARQL on the in-memory store, and called O(N²) times by the walker's
    property filter (where N≈66 for LIS-14)."""
    for inv in dataset.objects(prop, OWL.inverseOf):
        if isinstance(inv, URIRef):
            return inv
    for inv in dataset.subjects(OWL.inverseOf, prop):
        if isinstance(inv, URIRef):
            return inv
    return None


def parent_property(dataset: Graph, prop: URIRef) -> URIRef | None:
    """Return the most direct rdfs:subPropertyOf parent, or None.

    Direct iteration — same rationale as `inverse_of`."""
    for parent in dataset.objects(prop, RDFS.subPropertyOf):
        if isinstance(parent, URIRef):
            return parent
    return None


def properties_of(dataset: Graph, cls: URIRef, *, include_inherited: bool = True) -> list[URIRef]:
    """Return properties whose `rdfs:domain` is *cls* (or, when include_inherited,
    any super-class of *cls*).

    NOTE: this only returns DOMAIN-MATCHED properties. POSC's LIS-14 deliberately
    leaves most properties (~50 of 66) without `rdfs:domain` so they're
    universally applicable — those are returned by `domain_less_properties()`
    instead. The walker / extractable_properties_for() combines both.
    """
    domains = [cls]
    if include_inherited:
        domains.extend(superclasses(dataset, cls, direct=False))
    seen: set[URIRef] = set()
    for d in domains:
        for row in dataset.query(
            """
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            SELECT DISTINCT ?p WHERE { ?p rdfs:domain ?d . FILTER(!isBlank(?p)) }
            """,
            initBindings={"d": d},
        ):
            if isinstance(row[0], URIRef):
                seen.add(row[0])
    return sorted(seen, key=str)


def domain_less_properties(dataset: Graph, namespace: str | None = None) -> list[URIRef]:
    """Properties (object or datatype) with NO `rdfs:domain` declared — meant
    to be universally applicable. POSC's LIS-14 leaves ~50 of its 66 properties
    domain-less by design (e.g., `lis:approvedOn`, `lis:hasRole`, `lis:hasBeginning`,
    `lis:createdBy`, `lis:before`, `lis:after`). The LLM picks where they fit
    per-entity.

    *namespace* filters to properties in that URI prefix (typically the upper
    ontology's namespace, so non-LIS infrastructure properties don't surface).

    Implementation note: uses direct rdflib iteration rather than SPARQL
    `FILTER NOT EXISTS` — the latter is dramatically slower on rdflib's
    in-memory store (orders of magnitude on graphs of a few thousand triples).
    """
    seen: set[URIRef] = set()
    for prop_class in (OWL.ObjectProperty, OWL.DatatypeProperty):
        for p in dataset.subjects(RDF.type, prop_class):
            if not isinstance(p, URIRef):
                continue
            if p in seen:
                continue
            if namespace and not str(p).startswith(namespace):
                continue
            # Domain-less: no rdfs:domain triple at all
            if any(dataset.objects(p, RDFS.domain)):
                continue
            seen.add(p)
    return sorted(seen, key=str)


def is_object_property(dataset: Graph, prop: URIRef) -> bool:
    """True if *prop* is declared `rdf:type owl:ObjectProperty`.

    Used by the materializer to refuse literal values for object properties.
    A literal `lis:hasQuality "warm, scarlet"` triple is a modeling bug —
    the LLM should mint a separate Quality entity and point at it.
    """
    return (prop, RDF.type, OWL.ObjectProperty) in dataset


def domain_satisfied(
    dataset:        Graph,
    subject_types:  list[URIRef],
    predicate:      URIRef,
) -> bool:
    """True if *predicate*'s `rdfs:domain` (if any) is satisfied by at least
    one of *subject_types* (transitively via subClassOf).

    Used by the walker to validate property triples before adding them: an LLM
    that proposes `<lis:Person> lis:hasParticipant <X>` should be rejected
    because `lis:hasParticipant` has `rdfs:domain lis:Activity` and a Person
    isn't an Activity.

    Returns True (no constraint) when:
    - The predicate has no `rdfs:domain` declaration (universally applicable)
    - Or the predicate's domain is a blank node (complex domain like owl:unionOf
      — skip strict validation to avoid false positives)
    - Or the subject types include the domain or any subclass-ancestor of it
    """
    return _domain_or_range_satisfied(dataset, subject_types, predicate, RDFS.domain)


def range_satisfied(
    dataset:      Graph,
    object_types: list[URIRef],
    predicate:    URIRef,
) -> bool:
    """True if *predicate*'s `rdfs:range` (if any) is satisfied by at least
    one of *object_types* (transitively via subClassOf).

    Symmetric to `domain_satisfied`. Used to reject triples like
    `<organization> lis:representedBy <person>` where `lis:representedBy` has
    `rdfs:range lis:InformationObject` — Person isn't an InformationObject.

    Returns True (no constraint) when the predicate has no `rdfs:range`
    declaration, or its range is a blank node (complex range), or one of the
    object types satisfies the range.
    """
    return _domain_or_range_satisfied(dataset, object_types, predicate, RDFS.range)


def _domain_or_range_satisfied(
    dataset:      Graph,
    types:        list[URIRef],
    predicate:    URIRef,
    constraint:   URIRef,           # RDFS.domain or RDFS.range
) -> bool:
    declared = [c for c in dataset.objects(predicate, constraint)
                if isinstance(c, URIRef)]
    if not declared:
        return True
    extended: set[URIRef] = set(types)
    for t in types:
        for sup in superclasses(dataset, t, direct=False):
            extended.add(sup)
    return any(d in extended for d in declared)


def is_class_range(dataset: Graph, predicate: URIRef) -> bool:
    """True if *predicate*'s `rdfs:range` is an `owl:Class` (object property
    pointing at an entity), False if it's a datatype like `xsd:string`/etc.,
    or None if no range is declared.

    Used to detect "literal value where range expects a class entity" — a
    classic mismatch where the LLM emits a literal but the property expects
    an entity URI.
    """
    XSD_NS = "http://www.w3.org/2001/XMLSchema#"
    RDFS_NS = "http://www.w3.org/2000/01/rdf-schema#"
    declared = [c for c in dataset.objects(predicate, RDFS.range)
                if isinstance(c, URIRef)]
    if not declared:
        return False    # no range constraint → not a "class range"
    for r in declared:
        rs = str(r)
        if rs.startswith(XSD_NS):
            return False
        if rs == RDFS_NS + "Literal":
            return False
    return True


def domains_of(dataset: Graph, prop: URIRef) -> list[URIRef]:
    """All `rdfs:domain` classes declared for *prop*. Empty list = no domain.

    Used by the property-catalog renderer to show the LLM where a property
    is allowed — pre-flight guidance to reduce post-LLM domain violations.

    If *prop* has no declared domain, falls back to the inverse property's
    `rdfs:range` (which by inverse symmetry IS the domain of *prop*). LIS-14
    leaves many inverses (e.g. `lis:participantIn`) without explicit domain
    or range — the constraint lives only on the forward direction.
    """
    direct = sorted(
        {o for o in dataset.objects(prop, RDFS.domain) if isinstance(o, URIRef)},
        key=str,
    )
    if direct:
        return direct
    inv = inverse_of(dataset, prop)
    if inv is not None:
        inv_range = _range_direct(dataset, inv)
        if inv_range is not None:
            return [inv_range]
    return []


def range_of(dataset: Graph, prop: URIRef) -> URIRef | None:
    """Return the property's `rdfs:range` (first declared), or None.

    Falls back to the inverse property's `rdfs:domain` when no direct range
    is declared — same inverse-symmetry logic as `domains_of`.
    """
    direct = _range_direct(dataset, prop)
    if direct is not None:
        return direct
    inv = inverse_of(dataset, prop)
    if inv is not None:
        for o in dataset.objects(inv, RDFS.domain):
            if isinstance(o, URIRef):
                return o
    return None


def _range_direct(dataset: Graph, prop: URIRef) -> URIRef | None:
    """Direct `rdfs:range` lookup with no inverse fallback (internal)."""
    for o in dataset.objects(prop, RDFS.range):
        if isinstance(o, URIRef):
            return o
    return None


def class_label(dataset: Graph, cls: URIRef) -> str:
    """rdfs:label, falling back to the URI's local name."""
    for row in dataset.query(
        "SELECT ?l WHERE { ?cls <http://www.w3.org/2000/01/rdf-schema#label> ?l } LIMIT 1",
        initBindings={"cls": cls},
    ):
        return str(row[0])
    return _local_name(cls)


def class_definition(dataset: Graph, cls: URIRef) -> str:
    """skos:definition, falling back to rdfs:comment, then ''."""
    for prop in (SKOS.definition, RDFS.comment):
        for row in dataset.query(
            "SELECT ?d WHERE { ?cls ?prop ?d } LIMIT 1",
            initBindings={"cls": cls, "prop": prop},
        ):
            return str(row[0])
    return ""


# Property aliases for symmetry
property_label = class_label
property_definition = class_definition


def scope_notes(dataset: Graph, term: URIRef) -> list[str]:
    """All `skos:scopeNote` annotations on *term*.

    Scope notes carry behavioral guidance — clarifications about how a
    class or property should be USED — that go beyond the class's
    definition. The extraction prompts surface these directly to the LLM
    so per-class corrections can live in the ontology rather than baked
    into Python templates. Multiple notes per term are allowed and are
    rendered as separate lines.
    """
    out: list[str] = []
    for o in dataset.objects(term, SKOS.scopeNote):
        s = str(o).strip()
        if s:
            out.append(s)
    return out


def examples(dataset: Graph, term: URIRef) -> list[str]:
    """All `skos:example` annotations on *term*.

    Companion to `scope_notes` — concrete usage examples shown alongside
    the behavioral guidance. Multiple examples per term are allowed (each
    `skos:example` triple adds one); preserves no particular order.
    """
    out: list[str] = []
    for o in dataset.objects(term, SKOS.example):
        s = str(o).strip()
        if s:
            out.append(s)
    return out


def is_extractable(dataset: Graph, term: URIRef) -> bool:
    """Return False if *term* carries `dg:extractable false`, else True.

    Used by the walker to skip classes/properties marked as opt-out in
    `dg-part14-alignments.ttl` or any user-loaded ontology.
    """
    DG_EXTRACTABLE = URIRef("urn:docgraph:vocab:meta#extractable")
    for row in dataset.query(
        "SELECT ?v WHERE { ?term ?ext ?v }",
        initBindings={"term": term, "ext": DG_EXTRACTABLE},
    ):
        v = row[0]
        if str(v).lower() in ("false", "0"):
            return False
    return True


def _local_name(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s
