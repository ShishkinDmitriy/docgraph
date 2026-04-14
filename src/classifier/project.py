"""Project root discovery and .docgraph directory initialisation."""

import shutil
from pathlib import Path

from rdflib import Graph, Literal, Namespace, RDF
from rich.console import Console

DOCGRAPH_DIR      = ".docgraph"
REGISTRY_FILENAME = "docgraph.ttl"
ONTOLOGIES_SUBDIR = "ontologies"
CACHE_SUBDIR      = "cache"

# data/ is the single canonical source for both the registry and ontology files.
_DATA_DIR = Path(__file__).parent.parent.parent / "data"

_DOCGRAPH = Namespace("http://example.org/tax-classifier/docgraph#")

# Ontology files installed into .docgraph/ontologies/ by `init`.
_ONTOLOGY_FILES = [
    "financial_documents.ttl",
    "models.ttl",
    "shapes.ttl",
]


def find_project_root(start: Path | None = None) -> Path | None:
    """
    Walk up from *start* (default: cwd) looking for a directory that contains
    ``.docgraph/docgraph.ttl``.  Returns the project root or None.
    """
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / DOCGRAPH_DIR
        if candidate.is_dir() and (candidate / REGISTRY_FILENAME).is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def registry_path(project_root: Path) -> Path:
    """Return ``.docgraph/docgraph.ttl`` for *project_root*."""
    return project_root / DOCGRAPH_DIR / REGISTRY_FILENAME


def cache_dir(project_root: Path) -> Path:
    """Return ``.docgraph/cache/`` for *project_root*."""
    return project_root / DOCGRAPH_DIR / CACHE_SUBDIR


def _write_init_registry(src: Path, dst: Path) -> None:
    """
    Parse *src* (data/docgraph.ttl), rewrite relativePath values for the
    .docgraph/ layout, and serialise to *dst*.

    Local ontology paths become ``.docgraph/ontologies/<filename>``;
    the output path becomes ``.docgraph/results.ttl``.
    """
    g = Graph()
    g.parse(src)

    for subj in g.subjects(RDF.type, _DOCGRAPH.LocalOntology):
        rel = g.value(subj, _DOCGRAPH.relativePath)
        if rel is not None:
            fname = Path(str(rel)).name
            g.set((subj, _DOCGRAPH.relativePath,
                   Literal(f"{DOCGRAPH_DIR}/{ONTOLOGIES_SUBDIR}/{fname}")))

    for subj in g.subjects(RDF.type, _DOCGRAPH.Output):
        rel = g.value(subj, _DOCGRAPH.relativePath)
        if rel is not None:
            g.set((subj, _DOCGRAPH.relativePath,
                   Literal(f"{DOCGRAPH_DIR}/results.ttl")))

    g.serialize(destination=str(dst), format="turtle")


def init_project(target: Path, console: Console, *, force: bool = False) -> None:
    """
    Create a ``.docgraph/`` project directory inside *target*.

    Raises ``FileExistsError`` if ``.docgraph/`` already exists and *force* is
    False.
    """
    docgraph_dir   = target / DOCGRAPH_DIR
    ontologies_dir = docgraph_dir / ONTOLOGIES_SUBDIR
    cache_dir_path = docgraph_dir / CACHE_SUBDIR

    if docgraph_dir.exists() and not force:
        raise FileExistsError(
            f"{docgraph_dir} already exists. Use --force to reinitialise."
        )

    ontologies_dir.mkdir(parents=True, exist_ok=True)
    cache_dir_path.mkdir(parents=True, exist_ok=True)
    console.print(f"  created [dim]{docgraph_dir}[/dim]")

    # ── Registry ──────────────────────────────────────────────────────────────
    _write_init_registry(_DATA_DIR / REGISTRY_FILENAME, docgraph_dir / REGISTRY_FILENAME)
    console.print(f"  wrote   [dim]{REGISTRY_FILENAME}[/dim] → [dim].docgraph/[/dim]")

    # ── Ontology files ────────────────────────────────────────────────────────
    for fname in _ONTOLOGY_FILES:
        shutil.copy2(_DATA_DIR / fname, ontologies_dir / fname)
        console.print(f"  copied  [dim]{fname}[/dim] → [dim].docgraph/ontologies/[/dim]")

    console.print(
        f"\n[green]Initialised docgraph project in[/green] [bold]{target}[/bold]\n"
        f"Edit [dim].docgraph/docgraph.ttl[/dim] to configure ontologies and the output namespace."
    )
