"""extract — mega-walker LLM call: entities + properties + ext classes.

One LLM call produces entities, properties, evidence anchors, ext-class
proposals — assembled into a single delta. After the LLM result lands,
the deterministic cross-entity inference pass fills in obvious links
the LLM missed (UOM via quote co-occurrence, etc.).

Dirty check: clean iff convert has produced an HtmlFile AND extract's
latest delta seq ≥ convert's latest delta seq. The seq comparison is
what makes `-f convert` cascade — a fresh convert delta makes extract
see "my output is stale" and re-fire.
"""

from __future__ import annotations

from rdflib import Graph

from src.deltas import StepDelta, delta_path, doc_scope, next_seq, write_delta
from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.mega_walker import walk_mega
from src.extract_part14.property_walker import infer_cross_entity_links
from src.extract_part14.rdl import POSC_CAESAR, RdlResolver
from src.extract_part14.structural import DG
from src.tasks._helpers import (
    doc_state,
    has_delta_with_step,
    is_stale_wrt,
    now,
    print_delta_summary,
)
from src.tasks._registry import docgraph
from src.project import cache_dir
from rdflib.namespace import RDF


@docgraph.task(desc="Extract entities + properties via mega-walker LLM",
               deps=("load_html",))
def extract(ctx) -> None:
    console = ctx["console"]
    ds       = build_dataset(ctx["project_root"])
    ontology = union_view(ds)
    ctx["ontology"] = ontology

    g = Graph()
    extracted: list = []
    if ctx["full_markdown"].strip():
        rdl_cache_dir = cache_dir(ctx["project_root"]) / "rdl"
        rdl_resolvers = [RdlResolver(POSC_CAESAR, cache_dir=rdl_cache_dir)]
        result = walk_mega(
            full_markdown   = ctx["full_markdown"],
            document_title  = ctx["document_title"],
            document_descr  = ctx["document_description"],
            base_ns         = ctx["base_ns"],
            md_source_uri   = ctx["html_uri"],
            file_uri        = ctx["file_uri"],
            ontology        = ontology,
            client          = ctx["client"],
            model           = ctx["model"],
            id_to_class     = ctx["id_to_class"],
            class_to_ids    = ctx["class_to_ids"],
            rdl_resolvers   = rdl_resolvers,
            console         = console,
        )
        for triple in result.graph:
            g.add(triple)
        for prefix, ns in result.graph.namespaces():
            g.bind(prefix, ns, override=False)
        extracted = result.entities
        console.print(f"  → {len(extracted)} entit{'y' if len(extracted) == 1 else 'ies'}, "
                      f"{len(result.new_ext_classes)} new ext class(es)")

    if extracted:
        inferred = infer_cross_entity_links(extracted, g, ontology, console=console)
        for triple in inferred:
            g.add(triple)

    ctx["extracted"] = extracted

    if len(g) > 0:
        seq = next_seq(ctx["project_root"], doc_scope(ctx["slug"]))
        write_delta(
            StepDelta(scope=doc_scope(ctx["slug"]), step="extract", seq=seq,
                      added=g, parent_seq=seq - 1, agent=ctx["agent_uri"],
                      timestamp=now()),
            delta_path(ctx["project_root"], doc_scope(ctx["slug"]), seq),
        )
        print_delta_summary(console, seq, len(g), 0)


@docgraph.dirty
def extract_dirty(ctx) -> bool:
    if "full_markdown" not in ctx:
        return False                    # no HTML loaded — can't extract
    state = doc_state(ctx)
    if (ctx["html_uri"], RDF.type, DG.HtmlFile) not in state:
        return False
    return (not has_delta_with_step(ctx, "extract")
            or is_stale_wrt(ctx, "extract", ("convert",)))
