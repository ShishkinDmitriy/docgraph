"""Enrichment pass: refine entity types via RDL, then extract any new
properties unlocked by the more-specific types.

`extract` produces a Part 14 graph using only the upper ontology + loaded
domain ontologies. `enrich` is the optional follow-up that goes outside the
project — queries an external RDL for more specific class types, then
re-runs property extraction for any properties the new types unlock.

Layered model:

  Layer 1 (extract): file → doc + Part 14 typing                    — local
  Layer 2 (extract): per-branch entity extraction                   — local
  Layer 3 (extract): properties from the upper-ontology vocabulary  — local
  Layer 4 (enrich):  type refinement via RDL                        — external
  Layer 5 (enrich):  properties unlocked by the refined types       — local + external

Each layer is idempotent and additive — running enrich twice doesn't
duplicate triples. Failure of an external RDL doesn't undo prior layers.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD
from rich.console import Console

from src.extract_part14 import axioms
from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.property_walker import (
    coerce_value,
    extract_properties_for_entity,
    extractable_properties_for,
)
from src.extract_part14.rdl import RdlResolver
from src.extract_part14.walker import (
    DG,
    LIS,
    OA,
    EvidenceSelector,
    ExtractedEntity,
)
from src.llm import LLMClient
from src.models import ModelConfig
from src.project import graphs_dir

logger = logging.getLogger(__name__)


# ── Reading the source graph ───────────────────────────────────────────────

def _local_name(uri: URIRef) -> str:
    s = str(uri)
    for sep in ("#", "/"):
        if sep in s:
            return s.rsplit(sep, 1)[-1]
    return s


def find_typed_entities(
    source_graph: Graph,
    ontology:     Graph,
) -> list[ExtractedEntity]:
    """Find entities in *source_graph* with extractable types.

    Skips infrastructure/plumbing entities (anything whose only types are
    dg:* / prov:* / owl:* / rdfs:* — typically the file, document, quotes,
    activities, agent records).
    """
    entities: list[ExtractedEntity] = []
    seen: set[URIRef] = set()

    for s, _, o in source_graph.triples((None, RDF.type, None)):
        if not isinstance(s, URIRef) or not isinstance(o, URIRef):
            continue
        if s in seen:
            continue

        # All types this entity has, minus plumbing
        all_types = list(source_graph.objects(s, RDF.type))
        extractable_types = [
            t for t in all_types
            if isinstance(t, URIRef) and axioms.is_extractable(ontology, t)
        ]
        if not extractable_types:
            continue

        # Pick the "primary" type — most specific extractable LIS class if available,
        # else first extractable type
        primary = _most_specific(extractable_types, ontology)
        label_terms = list(source_graph.objects(s, RDFS.label))
        label = str(label_terms[0]) if label_terms else _local_name(s)

        # Recover supporting quotes for this entity
        evidence = _recover_evidence(source_graph, s)

        # Recover LLM-suggested specific class names for RDL probing
        type_hints = [
            str(o) for o in source_graph.objects(s, DG.typeHint)
            if isinstance(o, Literal)
        ]

        entities.append(ExtractedEntity(
            uri        = s,
            type_uri   = primary,
            label      = label,
            evidence   = evidence,
            types      = [t for t in extractable_types],
            type_hints = type_hints,
        ))
        seen.add(s)

    return entities


def _most_specific(types: list[URIRef], ontology: Graph) -> URIRef:
    """Pick the most specific class from a list — i.e. one with no other
    type in the list as a descendant. Falls back to the first type if no
    clear winner."""
    if len(types) == 1:
        return types[0]
    for t in types:
        descendants = set(axioms.subclasses(ontology, t, direct=False))
        if not (descendants & set(types)):
            return t
    return types[0]


def _recover_evidence(source_graph: Graph, entity_uri: URIRef) -> list[EvidenceSelector]:
    """Recover the entity's evidence from a serialized graph.

    Two shapes are supported:

    1. **Current** — `lis:representedBy <doc#id-N>` (fragment URI). The
       anchor is the local part after `#`. There's no `exact` text in the
       graph; the canonical HTML carries it. We populate `EvidenceSelector`
       with the anchor only.

    2. **Legacy** — `lis:representedBy <quote-uri>` where `quote-uri` is a
       `dg:Quote` with `oa:hasSelector` blank node carrying `oa:exact` /
       `oa:prefix` / `oa:suffix`. Pre-Phase-2 graphs use this shape. We
       extract `exact` and ignore prefix/suffix (no longer fields on
       EvidenceSelector).
    """
    selectors: list[EvidenceSelector] = []
    for ev_uri in source_graph.objects(entity_uri, LIS.representedBy):
        if not isinstance(ev_uri, URIRef):
            continue
        # (1) Fragment URI: <doc#anchor>. No oa:hasSelector triples on it.
        s = str(ev_uri)
        if "#" in s:
            anchor = s.rsplit("#", 1)[-1]
            if anchor:
                selectors.append(EvidenceSelector(exact="", anchor=anchor))
                continue
        # (2) Legacy dg:Quote shape — walk oa:hasSelector → oa:exact.
        for sel_node in source_graph.objects(ev_uri, OA.hasSelector):
            exact_terms = list(source_graph.objects(sel_node, OA.exact))
            if not exact_terms:
                continue
            selectors.append(EvidenceSelector(exact=str(exact_terms[0])))
    return selectors


# ── Type refinement ────────────────────────────────────────────────────────

@dataclass
class RefinementResult:
    new_types_per_entity: dict[URIRef, list[URIRef]]
    new_triples_count:    int


def refine_types(
    g:                Graph,
    entities:         list[ExtractedEntity],
    rdl_resolvers:    list[RdlResolver],
    *,
    ontology:         Graph | None = None,
    confidence_floor: float = 0.5,
    console:          Console | None = None,
) -> RefinementResult:
    """For each entity, query RDL resolvers using the entity's label as the
    probe; add confident matches as additional rdf:type triples.

    RDL scope: each resolver may declare `config.covers` (a tuple of upper-
    ontology classes it's competent for). Entities whose type isn't in that
    scope (transitively via subClassOf) skip the resolver entirely —
    avoids spam queries for Persons / Locations / etc. against an industrial
    RDL. Pass *ontology* (the loaded union view) to enable scope checking;
    when None, all RDLs are queried for all entities.

    Idempotent — skips types already present on the entity. Returns the map
    of newly-added types per entity (used downstream to find unlocked
    properties).
    """
    refined: dict[URIRef, list[URIRef]] = {}
    new_triples = 0
    skipped_out_of_scope = 0

    for entity in entities:
        added: list[URIRef] = []
        # Probe set: bare label first, then LLM-suggested type hints. Hints
        # let us catch role refinements ("patient" → pca:Patient) and
        # type-discovery ("EUR" → pca:Currency) without relying on the
        # entity's label happening to match a canonical RDL term.
        probes: list[str] = [entity.label]
        for hint in entity.type_hints:
            if hint and hint not in probes:
                probes.append(hint)

        for resolver in rdl_resolvers:
            # Scope filter: skip the resolver if it declares a `covers` set
            # and entity's type isn't (transitively) in it.
            if ontology is not None and resolver.config.covers and not _in_scope(
                ontology, entity.type_uri, resolver.config.covers
            ):
                skipped_out_of_scope += 1
                continue

            for probe in probes:
                hit = resolver.resolve(probe, kind_hint=entity.type_uri)
                if hit.uri is None or hit.confidence < confidence_floor:
                    continue
                # Idempotency
                if (entity.uri, RDF.type, hit.uri) in g:
                    continue
                g.add((entity.uri, RDF.type, hit.uri))
                new_triples += 1
                added.append(hit.uri)
                if console:
                    via = "" if probe == entity.label else f" [dim]via hint {probe!r}[/dim]"
                    console.print(f"  [bold]{entity.label}[/bold] → "
                                  f"[dim]+ {_curie(hit.uri)} (conf {hit.confidence:.2f}){via}[/dim]")

        if added:
            refined[entity.uri] = added

    if console and skipped_out_of_scope:
        console.print(f"  [dim]({skipped_out_of_scope} resolver-calls skipped "
                      f"as out-of-scope)[/dim]")

    return RefinementResult(new_types_per_entity=refined, new_triples_count=new_triples)


def _in_scope(ontology: Graph, entity_type: URIRef, covers) -> bool:
    """True if *entity_type* is in the resolver's `covers` set, transitively
    (entity_type is a subclass of one of the covered classes).
    """
    covers_set = set(covers)
    if entity_type in covers_set:
        return True
    # Walk supers
    for sup in axioms.superclasses(ontology, entity_type, direct=False):
        if sup in covers_set:
            return True
    return False


# ── Property unlock ───────────────────────────────────────────────────────

def extract_unlocked_properties(
    g:                Graph,
    entities:         list[ExtractedEntity],
    refined:          RefinementResult,
    ontology:         Graph,
    *,
    client:           LLMClient,
    model:            ModelConfig,
    rdl_resolvers:    list[RdlResolver] | None = None,
    document_context: str = "",
    console:          Console | None = None,
) -> int:
    """For each entity that gained new types, extract values for properties
    whose domain matches one of the new types but didn't apply before
    refinement. Skips properties already populated on the entity.

    Returns the number of new property triples added.
    """
    new_triples = 0
    by_uri = {e.uri: e for e in entities}

    for entity_uri, new_types in refined.new_types_per_entity.items():
        entity = by_uri.get(entity_uri)
        if entity is None:
            continue

        # Properties newly applicable via the refined types
        old_props = set(extractable_properties_for(entity.type_uri, ontology))
        new_props: set[URIRef] = set()
        for nt in new_types:
            new_props.update(extractable_properties_for(nt, ontology))
        new_props -= old_props
        # Also skip properties already populated on this entity
        new_props = {
            p for p in new_props
            if not any(g.triples((entity_uri, p, None)))
        }

        if not new_props:
            continue

        if console:
            console.print(f"  [dim]→ {entity.label}: "
                          f"{len(new_props)} new propert{'y' if len(new_props) == 1 else 'ies'} unlocked[/dim]")

        items, _invocations, notes = extract_properties_for_entity(
            entity           = entity,
            candidate_props  = list(new_props),
            ontology         = ontology,
            document_context = document_context,
            known_entities   = entities,
            client           = client,
            model            = model,
        )
        if notes and console:
            console.print(f"    [dim italic]notes: {notes}[/dim italic]")
        for item in items:
            range_uri = axioms.range_of(ontology, item.prop)
            value = coerce_value(
                item.result, range_uri, entities,
                rdl_resolvers=rdl_resolvers,
            )
            if value is not None:
                g.add((entity.uri, item.prop, value))
                new_triples += 1

    return new_triples


# ── Top-level entry point ─────────────────────────────────────────────────

def enrich_source(
    project_root:  Path,
    slug:          str,
    rdl_resolvers: list[RdlResolver],
    *,
    client:           LLMClient,
    model:            ModelConfig,
    confidence_floor: float = 0.5,
    console:          Console | None = None,
) -> int:
    """Enrich one source — writes a separate named graph next to extract.

    Storage shape (per source):
      .docgraph/graphs/<slug>.convert.ttl  ← convert layer (file/doc chain)
      .docgraph/graphs/<slug>.extract.ttl  ← extract layer (entities/properties/quotes)
      .docgraph/graphs/<slug>.enrich.ttl   ← this function's output

    Each file is its own named graph in the loaded Dataset (the loader's
    `union_view` flattens them for queries). Cascade-deleting just the
    enrich layer is `rm <slug>.enrich.ttl` — convert/extract are untouched.

    Idempotency: the function reads convert+extract+enrich into a working
    graph, so types/properties already added by a prior enrich run are
    skipped, and rewriting `<slug>.enrich.ttl` produces deterministic content.

    Returns the total number of new triples added in THIS run (delta vs.
    the previous enrich state).
    """
    extract_path = graphs_dir(project_root) / f"{slug}.extract.ttl"
    convert_path = graphs_dir(project_root) / f"{slug}.convert.ttl"
    enrich_path  = graphs_dir(project_root) / f"{slug}.enrich.ttl"

    if not extract_path.is_file():
        raise FileNotFoundError(f"no extract graph at {extract_path}")

    # Read-only baseline: convert + extract layers. The delta we'll write
    # to enrich.ttl is computed against this baseline (so anything inherited
    # from the source's earlier stages doesn't end up duplicated in enrich).
    baseline = Graph()
    if convert_path.is_file():
        baseline.parse(convert_path, format="turtle")
    baseline.parse(extract_path, format="turtle")

    # Working graph: baseline + any prior enrich content. Idempotency lookups
    # ('is this type already there?') see the union; new triples land here.
    working = Graph()
    for t in baseline:
        working.add(t)
    if enrich_path.is_file():
        working.parse(enrich_path, format="turtle")

    ds = build_dataset(project_root)
    ontology = union_view(ds)

    entities = find_typed_entities(working, ontology)
    if console:
        console.print(f"  found [bold]{len(entities)}[/bold] typed entit{'y' if len(entities) == 1 else 'ies'} to refine")
    if not entities:
        return 0

    if console:
        console.print(f"  [bold]refining types[/bold] via {len(rdl_resolvers)} RDL resolver(s)...")
    refinement = refine_types(
        working, entities, rdl_resolvers,
        ontology=ontology,
        confidence_floor=confidence_floor,
        console=console,
    )
    if console:
        console.print(f"  → {refinement.new_triples_count} type triple(s) added")

    prop_triples = 0
    if refinement.new_types_per_entity:
        if console:
            console.print(f"  [bold]extracting unlocked properties[/bold]...")
        prop_triples = extract_unlocked_properties(
            working, entities, refinement, ontology,
            client=client, model=model,
            rdl_resolvers=rdl_resolvers,
            document_context="",
            console=console,
        )
        if console:
            console.print(f"  → {prop_triples} property triple(s) added")

    # Compute delta = working - baseline — that's everything the enrich layer
    # contributes (new types, new properties, plus anything carried over from
    # a prior enrich run).
    delta = Graph()
    delta.bind("dg",   DG)
    delta.bind("lis",  LIS)
    delta.bind("oa",   OA)
    delta.bind("rdfs", RDFS)
    delta.bind("xsd",  XSD)
    # Carry source-graph prefix bindings so the delta reads cleanly
    for prefix, ns in baseline.namespaces():
        delta.bind(prefix, ns, override=False)
    for t in working:
        if t not in baseline:
            delta.add(t)

    # Always write the file — even if delta is empty, an explicit empty
    # enrich.ttl signals "this source has been enriched and produced nothing
    # new." Skip writing only if there's no prior file AND nothing to write.
    if len(delta) > 0 or enrich_path.is_file():
        delta.serialize(destination=str(enrich_path), format="turtle")
        if console:
            console.print(f"  wrote   [dim]{enrich_path.name}[/dim] ({len(delta)} triples in enrich layer)")

    return refinement.new_triples_count + prop_triples


def _curie(uri: URIRef) -> str:
    s = str(uri)
    for ns, prefix in (
        ("http://rds.posccaesar.org/ontology/lis14/rdl/", "lis"),
        ("http://example.org/docgraph/meta#",          "dg"),
        ("http://www.w3.org/ns/oa#",                   "oa"),
        ("http://www.w3.org/ns/prov#",                 "prov"),
        ("http://www.wikidata.org/entity/",            "wd"),
    ):
        if s.startswith(ns):
            return f"{prefix}:{s[len(ns):]}"
    return f"<{s}>"
