"""Build the in-memory rdflib Dataset for a part14 project.

Implements the loader recipe from ARCHITECTURE.md § Storage layout — reads
config.ttl, loads bundled foundationals from vendor/ontologies/, loads
per-source graphs from .docgraph/graphs/. No copying into .docgraph/.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Dataset, Graph, Namespace, URIRef

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
      - One named graph per bundled foundational ontology (URI: urn:docgraph:foundational/<slug>)
      - One named graph per registered source (URI: from sources.ttl's dg:graphFile)
      - sources.ttl as the default graph (so registry queries are convenient)
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

    # 5. Each registered per-source graph.
    g_dir = graphs_dir(project_root)
    if g_dir.is_dir():
        for ttl in sorted(g_dir.glob("*.ttl")):
            graph_uri = URIRef(f"urn:docgraph:source/{ttl.stem}")
            g = ds.graph(graph_uri)
            g.parse(ttl, format="turtle")

    return ds


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
