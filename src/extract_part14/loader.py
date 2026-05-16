"""Build the in-memory rdflib Dataset for a part14 project.

Implements the loader recipe from ARCHITECTURE.md § Storage layout — reads
config.ttl, loads bundled foundationals from vendor/ontologies/, loads
per-source graphs from .docgraph/graphs/. No copying into .docgraph/.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Dataset, Graph, Namespace, URIRef

from src.deltas import (
    Scope,
    list_scopes,
    materialize,
)
from src.project import (
    PIPELINE_PART14,
    config_path,
    ext_ontology_path,
    graphs_dir,
    read_pipeline,
    sources_path,
)

# Bundled foundationals for the part14 pipeline.
_VENDOR_ONTOLOGIES_DIR = Path(__file__).parent.parent.parent / "vendor" / "ontologies"

_BUNDLED = [
    ("LIS-14.ttl",                 "ttl",     "lis"),
    ("dg.ttl",                     "ttl",     "dg"),
    ("dg-part14-alignments.ttl",   "ttl",     "dg-part14-alignments"),
    ("tpl.ttl",                    "ttl",     "tpl"),
    ("prov-o.ttl",                 "ttl",     "prov-o"),
    ("oa.ttl",                     "ttl",     "oa"),
    ("dcterms.ttl",                "ttl",     "dcterms"),
]

# Graph URI prefix for bundled foundationals — keeps each ontology in its own
# named graph so SPARQL queries can scope by source.
_FOUNDATIONAL_GRAPH_NS = Namespace("urn:docgraph:foundational/")


class LoaderError(Exception):
    pass


def build_dataset(project_root: Path) -> Dataset:
    """Assemble the project's full rdflib Dataset.

    Returns a Dataset containing:
      - One named graph per bundled foundational ontology
        (URI: urn:docgraph:foundational/<slug>)
      - One named graph per delta scope, materialized from the
        scope's `.trig` deltas in seq order
        (URI: urn:docgraph:scope/<kind>[/<name>])
      - One named graph per non-redundant HEAD snapshot in
        `.docgraph/graphs/*.ttl` (URI: urn:docgraph:source/<stem>).
        `<slug>.convert.ttl` and `<slug>.extract.ttl` are skipped when
        the doc slug already has deltas loaded (no double-loading).
        `<slug>.templates.ttl` is always loaded — templates phase
        isn't yet deltized.
      - sources.ttl + config.ttl + ext.ttl as appropriate.
    """
    pipeline = read_pipeline(project_root)
    if pipeline != PIPELINE_PART14:
        raise LoaderError(
            f"build_dataset is for part14 projects, but {project_root} "
            f"declares pipeline={pipeline!r}. Use the part2 loader path."
        )

    ds = Dataset()

    # 1. Bundled foundationals — one named graph each.
    for fname, fmt, slug in _BUNDLED:
        path = _VENDOR_ONTOLOGIES_DIR / fname
        if not path.is_file():
            raise LoaderError(
                f"Bundled foundational not found at {path}. "
                "docgraph install is incomplete or vendor/ontologies/ was moved."
            )
        graph_uri = URIRef(_FOUNDATIONAL_GRAPH_NS[slug])
        g = ds.graph(graph_uri)
        g.parse(path, format=fmt)

    # 2. config.ttl into the default graph (small; per-project metadata).
    cfg = config_path(project_root)
    if cfg.is_file():
        ds.default_graph.parse(cfg, format="turtle")

    # 3. sources.ttl into the default graph (registry).
    sp = sources_path(project_root)
    if sp.is_file():
        ds.default_graph.parse(sp, format="turtle")

    # 4. Per-project extension ontology — LLM-proposed classes.
    ext_path = ext_ontology_path(project_root)
    if ext_path.is_file():
        graph_uri = URIRef(_FOUNDATIONAL_GRAPH_NS["ext"])
        g = ds.graph(graph_uri)
        g.parse(ext_path, format="turtle")

    # 5. Versioned-graph deltas — for every scope that has at least one
    #    delta file (`<scope-prefix>.NNN.trig`), materialize the current
    #    state and load it as a named graph at the scope's canonical URI.
    #    Track which doc slugs are covered so we can skip their
    #    redundant HEAD snapshots in step 6.
    g_dir = graphs_dir(project_root)
    doc_slugs_with_deltas: set[str] = set()
    if g_dir.is_dir():
        for scope in list_scopes(g_dir):
            materialized = materialize(g_dir, scope)
            if len(materialized) == 0:
                continue
            g = ds.graph(scope.uri)
            for triple in materialized:
                g.add(triple)
            if scope.kind == "doc" and scope.name:
                doc_slugs_with_deltas.add(scope.name)

    # 6. Per-source HEAD snapshot graphs — legacy support only. The
    #    pipeline no longer writes .convert.ttl / .extract.ttl /
    #    .templates.ttl automatically (use `docgraph snapshot` to
    #    materialize on demand). For docs whose deltas are already
    #    loaded we SKIP any such legacy snapshots to avoid double-
    #    loading. Other .ttl files (foundationals dropped in graphs/,
    #    user-curated data, on-demand snapshots) load as before.
    if g_dir.is_dir():
        for ttl in sorted(g_dir.glob("*.ttl")):
            if _is_redundant_snapshot(ttl.name, doc_slugs_with_deltas):
                continue
            graph_uri = URIRef(f"urn:docgraph:source/{ttl.stem}")
            g = ds.graph(graph_uri)
            g.parse(ttl, format="turtle")

    return ds


def _is_redundant_snapshot(filename: str, doc_slugs_with_deltas: set[str]) -> bool:
    """True if `filename` is a legacy auto-snapshot (.convert.ttl,
    .extract.ttl, .templates.ttl) OR an on-demand snapshot
    (.<seq>.snapshot.ttl / .HEAD.snapshot.ttl) for a slug that already
    has delta files loaded. Skipping avoids double-counting the same
    triples (which RDF set semantics would dedupe but it's wasteful).
    """
    for suffix in (".convert.ttl", ".extract.ttl", ".templates.ttl",
                   ".HEAD.snapshot.ttl"):
        if filename.endswith(suffix):
            slug = filename[: -len(suffix)]
            if slug in doc_slugs_with_deltas:
                return True
    # `.<seq>.snapshot.ttl` — match the digit-only seq before .snapshot.ttl
    if filename.endswith(".snapshot.ttl"):
        stem = filename[: -len(".snapshot.ttl")]
        # split off potential trailing `.<seq>` (3+ digits) — leaves the slug
        for sep in ".":
            parts = stem.rsplit(sep, 1)
            if len(parts) == 2 and parts[1].isdigit():
                slug = parts[0]
                if slug in doc_slugs_with_deltas:
                    return True
    return False


def foundational_graph_uri(slug: str) -> URIRef:
    """Return the named-graph URI under which a bundled foundational lives."""
    return URIRef(_FOUNDATIONAL_GRAPH_NS[slug])


def union_view(dataset: Dataset) -> Graph:
    """Build a single rdflib Graph that's the flat union of every named graph
    in *dataset*. Use for SPARQL queries that need to span all loaded graphs.

    The Dataset itself preserves named-graph identity for serialization and
    cascade-delete; the union view is for read-only ontology / structural
    queries (axioms, type lookups) where the source graph doesn't matter.
    """
    g = Graph()
    for sub in dataset.graphs():
        for triple in sub:
            g.add(triple)
    return g
