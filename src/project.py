"""Project root discovery and .docgraph directory initialisation.

Layout (per ARCHITECTURE.md):

    .docgraph/
      config.ttl                       — project header (Part 14 pipeline marker)
      sources.ttl                      — registry of ingested sources
      templates.ttl                    — user-template registry
      docs/<slug>/
        delta.NNN.trig                 — versioned-graph deltas
        converted.html                 — PDF→HTML conversion (or converted.<part>.html)
        converted.md                   — markdown projection fed to the LLM
        annotated.html                 — derived viewer artifact (`docgraph view`)
        graph.ttl                      — HEAD snapshot (`docgraph snapshot`)
        graph.NNN.ttl                  — historical snapshots (`--at N`)
        diagram.{puml,svg,png}         — HEAD diagram (`docgraph diagram`)
        diagram.NNN.{puml,svg,png}     — historical diagrams (`snapshot --at N`)
      project/                         — project-scope deltas (promoted ext classes)
      rdl/<id>/                        — cached remote RDL deltas
      cache/                           — PDF→Markdown intermediate cache
"""

import shutil
from pathlib import Path

from rich.console import Console

DOCGRAPH_DIR                     = ".docgraph"
CONFIG_FILENAME                  = "config.ttl"          # project header
SOURCES_FILENAME                 = "sources.ttl"
CACHE_SUBDIR                     = "cache"
ONTOLOGIES_SUBDIR                = "ontologies"
EXT_FILENAME                     = "ext.ttl"

# Legacy flat `graphs/` directory — still used by the `docgraph enrich`
# step and the loader's legacy fallback. New per-source artifacts live
# under `docs/<slug>/` per the per-scope layout below.
GRAPHS_SUBDIR                    = "graphs"

# Per-scope grouping (current layout).
DOCS_SUBDIR                      = "docs"        # docs/<slug>/...
PROJECT_SCOPE_SUBDIR             = "project"     # project/...
RDL_SCOPE_SUBDIR                 = "rdl"         # rdl/<id>/...

# Filenames inside a scope's dir.
CONVERTED_HTML_FILENAME          = "converted.html"   # PDF→HTML conversion output
CONVERTED_MD_FILENAME            = "converted.md"     # markdown projection of converted.html
ANNOTATED_HTML_FILENAME          = "annotated.html"   # derived viewer artifact
GRAPH_TTL_FILENAME               = "graph.ttl"        # HEAD-state snapshot (Turtle)
DIAGRAM_BASENAME                 = "diagram"          # diagram.{puml,svg,png}


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for a directory that contains
    ``.docgraph/sources.ttl``. Returns the project root or None."""
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / DOCGRAPH_DIR / SOURCES_FILENAME).is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def docgraph_dir(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR


def config_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / CONFIG_FILENAME


def sources_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / SOURCES_FILENAME


def cache_dir(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / CACHE_SUBDIR


def graphs_dir(project_root: Path) -> Path:
    """Legacy `.docgraph/graphs/` — used by the (dormant) enrich step
    that still writes flat .ttl files. Current code uses `doc_dir(slug)`
    per the per-scope layout."""
    return project_root / DOCGRAPH_DIR / GRAPHS_SUBDIR


# ── Per-scope dirs (one dir per scope kind + name) ─────────────────────────


def doc_dir(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/` — every artifact for one doc lives here:
    delta.NNN.trig (versioned deltas), converted.html (PDF→HTML), converted.md
    (LLM-prompt view), annotated.html (derived viewer), graph[.NNN].ttl
    (on-demand snapshots), diagram[.NNN].{puml,svg,png} (on-demand diagrams).
    Easy to `rm -rf` a single doc."""
    return project_root / DOCGRAPH_DIR / DOCS_SUBDIR / slug


def project_scope_dir(project_root: Path) -> Path:
    """`.docgraph/project/` — project-scope deltas (e.g. promoted ext
    classes), parallel structure to doc dirs."""
    return project_root / DOCGRAPH_DIR / PROJECT_SCOPE_SUBDIR


def rdl_scope_dir(project_root: Path, rdl_id: str) -> Path:
    """`.docgraph/rdl/<id>/` — cached remote RDL data as deltas."""
    return project_root / DOCGRAPH_DIR / RDL_SCOPE_SUBDIR / rdl_id


# ── Per-doc artifact paths (the typed files inside a doc_dir) ──────────────


def converted_html_path(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/converted.html` — PDF→HTML conversion output.
    Source of truth for structure + atomic-unit IDs. Single-document PDFs
    use this exact path; multi-document PDFs add `converted.<part>.html`
    siblings (discovered via glob)."""
    return doc_dir(project_root, slug) / CONVERTED_HTML_FILENAME


def converted_md_path(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/converted.md` — markdown projection of
    converted.html that's fed to the extract LLM. Cached on disk so the
    prompt is reproducible + inspectable."""
    return doc_dir(project_root, slug) / CONVERTED_MD_FILENAME


def annotated_html_path(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/annotated.html` — derived viewer artifact
    from `docgraph view <slug>`. Regenerable any time."""
    return doc_dir(project_root, slug) / ANNOTATED_HTML_FILENAME


def graph_ttl_path(project_root: Path, slug: str, *,
                    at_seq: int | None = None) -> Path:
    """`.docgraph/docs/<slug>/graph.ttl` (HEAD) or `graph.NNN.ttl` (at seq).

    Written by `docgraph snapshot`. The HEAD snapshot is what the doc's
    graph looks like after every delta is applied; numbered snapshots
    freeze a historical state for diffing or sharing."""
    name = GRAPH_TTL_FILENAME if at_seq is None else f"graph.{at_seq:03d}.ttl"
    return doc_dir(project_root, slug) / name


def diagram_path(project_root: Path, slug: str, *,
                  fmt: str = "puml", at_seq: int | None = None) -> Path:
    """`.docgraph/docs/<slug>/diagram.<fmt>` (HEAD) or
    `diagram.NNN.<fmt>` (at seq).

    Written by `docgraph diagram` (HEAD) and `docgraph snapshot --at N`
    (numbered). *fmt* is one of {"puml", "svg", "png"}."""
    if at_seq is None:
        name = f"{DIAGRAM_BASENAME}.{fmt}"
    else:
        name = f"{DIAGRAM_BASENAME}.{at_seq:03d}.{fmt}"
    return doc_dir(project_root, slug) / name


def ontologies_dir(project_root: Path) -> Path:
    """Per-project mutable ontologies directory.

    Currently holds `ext.ttl` — LLM-proposed extension classes anchored
    under stable LIS-14 superclasses. Distinct from the immutable bundled
    foundationals in `vendor/ontologies/`. Loaded by the part14 loader
    into its own named graph so it's visible to extraction and enrich."""
    return project_root / DOCGRAPH_DIR / ONTOLOGIES_SUBDIR


def ext_ontology_path(project_root: Path) -> Path:
    """Path to the per-project extension ontology (LLM-proposed classes)."""
    return ontologies_dir(project_root) / EXT_FILENAME


def embeddings_path(project_root: Path) -> Path:
    """Path to the project-wide embedding store (`.docgraph/embeddings.npz`).

    Used by the ext-class dedup phase to detect near-duplicate proposed
    classes across docs (e.g. ext:Bill collapsing into an existing
    ext:Invoice when their label/comment embeddings are close).
    """
    return project_root / DOCGRAPH_DIR / "embeddings.npz"


_SOURCES_TTL = """\
@prefix dg:       <urn:docgraph:vocab:meta#> .
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix xsd:      <http://www.w3.org/2001/XMLSchema#> .

# Registry of ingested sources. Each record is dual-typed as
# dg:IngestionRecord (admin) and iso15926:WholeLifeIndividual (the file itself).
"""

# Minimal per-project header. No copies of foundational ontologies —
# the loader reads them from vendor/ontologies/ at startup.
# See ARCHITECTURE.md § Storage layout.
_CONFIG_TTL = """\
@prefix dg:  <urn:docgraph:vocab:meta#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<> a dg:DocgraphProject ;
    dg:createdAt  "{created_at}"^^xsd:date ;
    dg:version    "0.1.0" .
"""

_TEMPLATES_REGISTRY_TTL = """\
@prefix dg:  <urn:docgraph:vocab:meta#> .

# Registry of user-authored templates loaded by this project.
# Each entry: a dg:TemplateRegistration with dg:templatePath pointing at a TTL
# file in the project repo (typically under data/templates/<custom>/).
# Bundled templates (data/templates/iso14/, data/templates/bridges/) and the
# core tpl: vocabulary are not registered here — the loader picks them up
# automatically.
"""


def reset_sources(project_root: Path) -> None:
    """Overwrite sources.ttl with an empty registry (header only)."""
    sources_path(project_root).write_text(_SOURCES_TTL)


def init_project(
    target: Path,
    console: Console,
    *,
    force: bool = False,
) -> None:
    """Create the ``.docgraph/`` directory inside *target*.

    Raises ``FileExistsError`` if ``.docgraph/`` already exists and *force* is False.
    """
    dg_dir   = target / DOCGRAPH_DIR
    docs_dir = dg_dir / DOCS_SUBDIR
    c_dir    = dg_dir / CACHE_SUBDIR

    if dg_dir.exists() and not force:
        raise FileExistsError(f"{dg_dir} already exists. Use --force to reinitialise.")
    if dg_dir.exists() and force:
        shutil.rmtree(dg_dir)

    dg_dir.mkdir(parents=True)
    docs_dir.mkdir()
    c_dir.mkdir()
    console.print(f"  created [dim]{dg_dir}[/dim]")

    # Tiny config.ttl + empty templates registry. The loader reads
    # foundationals from vendor/ontologies/ at startup.
    from datetime import date
    (dg_dir / CONFIG_FILENAME).write_text(
        _CONFIG_TTL.format(created_at=date.today().isoformat())
    )
    console.print(f"  wrote   [dim]{CONFIG_FILENAME}[/dim]")
    (dg_dir / "templates.ttl").write_text(_TEMPLATES_REGISTRY_TTL)
    console.print(f"  wrote   [dim]templates.ttl[/dim]")

    (dg_dir / SOURCES_FILENAME).write_text(_SOURCES_TTL)
    console.print(f"  wrote   [dim]{SOURCES_FILENAME}[/dim]")

    console.print(
        f"\n[green]Initialised docgraph project in[/green] [bold]{target}[/bold]\n"
        f"Add a source with [dim]docgraph add <file>[/dim]."
    )
