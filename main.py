#!/usr/bin/env python3
"""DocGraph CLI."""

import logging
import os
import sys
from pathlib import Path

import click
from rdflib import URIRef
from rich.console import Console
from rich.table import Table

from src.ingest import TTL_SUFFIXES, IngestError, ingest_ttl, list_sources
from src.ingest_pdf import ingest_pdf
from src.llm.anthropic import AnthropicClient
from src.markdown_io import load_or_extract, md_paths_for_pdf
from src.models import ModelConfig
from src.project import (
    DEFAULT_PIPELINE,
    PIPELINE_PART14,
    PIPELINES,
    UNRESOLVED_FILENAME,
    cache_dir,
    find_project_root,
    graphs_dir,
    init_project,
    read_pipeline,
    reset_sources,
)

# Hardcoded vision model for PDF→Markdown conversion. Make this configurable
# (config.ttl in the project, or a CLI flag) once we have more than one option.
DEFAULT_VISION_MODEL = ModelConfig(
    uri=URIRef("http://example.org/docgraph/agent/claude-haiku-4-5"),
    model_id="claude-haiku-4-5",
    label="Claude Haiku 4.5",
    provider="anthropic",
)

console = Console()


@click.group()
def cli():
    """Build a knowledge graph from documents using ISO 15926 Part 14."""


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=None, required=False)
@click.option("--force", "-f", is_flag=True, help="Reinitialise even if .docgraph/ already exists.")
@click.option("--pipeline", type=click.Choice(PIPELINES), default=DEFAULT_PIPELINE,
              show_default=True,
              help="Upper-ontology pipeline. 'part2' is the legacy ISO 15926 Part 2 "
                   "pipeline (current default; copies foundationals into .docgraph/). "
                   "'part14' is the new ISO 15926 Part 14 pipeline (writes config.ttl "
                   "only; loader reads foundationals from vendor/ontologies/).")
def init(directory: Path | None, force: bool, pipeline: str):
    """Initialise a .docgraph/ project directory (analogous to git init)."""
    target = (directory or Path.cwd()).resolve()
    if not target.is_dir():
        console.print(f"[red]Error:[/red] {target} is not a directory.")
        sys.exit(1)
    try:
        init_project(target, console, force=force, pipeline=pipeline)
    except FileExistsError as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=None, required=False)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def clean(directory: Path | None, yes: bool):
    """Remove every ingested source: wipe graphs/*.ttl and reset sources.ttl.

    Leaves meta.ttl, lis-14.ttl, and the cache untouched. Use this to start the
    graph over without re-running `docgraph init`.
    """
    project_root = find_project_root((directory or Path.cwd()).resolve())
    if project_root is None:
        console.print("[red]Error:[/red] not a docgraph project (run `docgraph init`).")
        sys.exit(1)

    g_dir = graphs_dir(project_root)
    targets = sorted(p for p in g_dir.iterdir()
                     if p.suffix in (".ttl", ".trig") and p.name != UNRESOLVED_FILENAME)

    if not targets:
        console.print("[dim]Nothing to clean.[/dim]")
        return

    console.print(f"Will remove [bold]{len(targets)}[/bold] ingested graph(s):")
    for p in targets:
        console.print(f"  [dim]{p.relative_to(project_root)}[/dim]")

    if not yes:
        click.confirm("Proceed?", abort=True)

    for p in targets:
        p.unlink()  # works for both files and symlinks

    reset_sources(project_root)

    from src.embeddings import EMBEDDINGS_FILENAME
    from src.project import DOCGRAPH_DIR
    emb_path = project_root / DOCGRAPH_DIR / EMBEDDINGS_FILENAME
    if emb_path.is_file():
        emb_path.unlink()
        console.print(f"  also removed [dim]{emb_path.relative_to(project_root)}[/dim]")

    console.print(f"[green]Cleaned[/green] {len(targets)} graph(s) and reset sources.ttl")


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if debug:
        logging.getLogger("src").setLevel(logging.DEBUG)


def _find_project(source: Path) -> Path:
    project_root = find_project_root(source.parent) or find_project_root(Path.cwd())
    if project_root is None:
        console.print("[red]Error:[/red] not a docgraph project (run `docgraph init`).")
        sys.exit(1)
    console.print(f"Project root: [dim]{project_root}[/dim]")
    return project_root


def _anthropic_client():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Error:[/red] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)
    return AnthropicClient(api_key=api_key)


def _ingest_pdf_dispatched(project_root: Path, source: Path, **kwargs):
    """Route to the pipeline configured for this project."""
    pipeline = read_pipeline(project_root)
    if pipeline == PIPELINE_PART14:
        from src.extract_part14.pipeline import extract_pdf_part14
        return extract_pdf_part14(source, project_root, console, **kwargs)
    return ingest_pdf(source, project_root, console, **kwargs)


@cli.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--note", type=str, default=None,
              help="Free-text hint passed to the converter.")
@click.option("-f", "--force", is_flag=True,
              help="Re-run conversion even if cached markdown exists.")
@click.option("--debug", is_flag=True, help="Log every LLM prompt and response.")
def convert(input_path: Path, note: str | None, force: bool, debug: bool):
    """Convert a PDF source to Markdown and cache the result.

    First stage of the extraction pipeline (see ARCHITECTURE.md § Extraction
    pipeline — a decision tree). Output lands in `.docgraph/cache/pdfmd/`.
    Subsequent `docgraph extract` and `docgraph add` invocations reuse this
    cache, so iterating on classify/extract logic doesn't re-run the
    expensive vision-LLM call.

    For .ttl/.n3 sources this is a no-op (the file is already its own
    representation).
    """
    _setup_logging(debug)
    source = input_path.resolve()
    project_root = _find_project(source)

    suffix = source.suffix.lower()
    if suffix in TTL_SUFFIXES:
        console.print(f"  [dim]TTL source — no conversion needed[/dim]")
        return
    if suffix != ".pdf":
        console.print(f"[yellow]Not yet supported:[/yellow] convert from {suffix!r}.")
        sys.exit(2)

    client = _anthropic_client()
    cache = cache_dir(project_root)
    cache.mkdir(parents=True, exist_ok=True)
    if force:
        for md in md_paths_for_pdf(source, cache):
            md.unlink()
            console.print(f"  [yellow]--force[/yellow]: dropped cache "
                          f"[dim]{md.name}[/dim]")
    docs = load_or_extract(
        source, force=force, client=client, model=DEFAULT_VISION_MODEL,
        con=console, note=note, cache_dir=cache,
    )
    console.print(f"  cached [bold]{len(docs)}[/bold] markdown document(s)")


@cli.command()
@click.argument("target", required=False, default=None)
@click.option("--all", "all_sources", is_flag=True,
              help="Enrich every registered source.")
@click.option("--debug", is_flag=True, help="Log every LLM prompt and response.")
def enrich(target: str | None, all_sources: bool, debug: bool):
    """Refine entity types via external RDL and extract any newly-unlocked
    properties.

    Operates on already-extracted Part 14 graphs (built by `docgraph add` or
    `docgraph extract`). Idempotent — re-running adds nothing new if every
    refinement has already been applied.

    Currently uses Wikidata as the RDL POC; layered RDL configuration
    (per-source declarations + multi-RDL federation) lands later.
    """
    _setup_logging(debug)
    project_root = _find_project(Path.cwd())

    pipeline = read_pipeline(project_root)
    if pipeline != PIPELINE_PART14:
        console.print(f"[red]Error:[/red] enrich is only available for "
                      f"part14 projects (this project's pipeline is {pipeline!r}).")
        sys.exit(1)

    if target is None and not all_sources:
        console.print("[red]Error:[/red] specify a slug or pass --all.")
        sys.exit(1)

    from src.extract_part14.enrich import enrich_source
    from src.extract_part14.rdl import POSC_CAESAR, RdlResolver

    # POSC Caesar — Part 14-aligned RDL.
    rdl_cache_dir = cache_dir(project_root) / "rdl"
    rdl_resolvers = [RdlResolver(POSC_CAESAR, cache_dir=rdl_cache_dir)]

    client = _anthropic_client()

    targets: list[str]
    if all_sources:
        # Pick up every source that has an extract layer to enrich
        targets = sorted(
            p.name[: -len(".extract.ttl")]
            for p in graphs_dir(project_root).glob("*.extract.ttl")
        )
    else:
        targets = [_resolve_slug(project_root, target)]

    total = 0
    for slug in targets:
        console.print(f"[bold]enrich[/bold] {slug}")
        try:
            added = enrich_source(
                project_root, slug, rdl_resolvers,
                client=client, model=DEFAULT_VISION_MODEL,
                console=console,
            )
            total += added
        except FileNotFoundError as exc:
            console.print(f"  [yellow]skip[/yellow]: {exc}")
    console.print(f"\n[green]Done[/green] — {total} new triple(s) across {len(targets)} source(s).")


@cli.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--debug", is_flag=True, help="Log every LLM prompt and response.")
def extract(input_path: Path, debug: bool):
    """Re-run extraction (classify + 14-aspect pipeline) for a source.

    Reuses cached markdown if present (use `docgraph convert --force` first
    to refresh the markdown). Drops the existing graph entry for this source
    and rewrites it from cached markdown.

    NOTE: For the Part 2 pipeline this is currently the same as
    `docgraph add --force` — Q1/Q2 classify and 14-aspect extract are not
    yet split into separate commands. The `classify` command will land
    alongside `src/extract_part14/` (see ARCHITECTURE.md § Pipelines).
    """
    _setup_logging(debug)
    source = input_path.resolve()
    project_root = _find_project(source)

    suffix = source.suffix.lower()
    if suffix in TTL_SUFFIXES:
        console.print("[yellow]extract is a no-op for .ttl sources[/yellow] — "
                      "use `docgraph add --force` to re-register.")
        return
    if suffix != ".pdf":
        console.print(f"[yellow]Not yet supported:[/yellow] extract from {suffix!r}.")
        sys.exit(2)

    client = _anthropic_client()
    try:
        _ingest_pdf_dispatched(project_root, source,
                               client=client, model=DEFAULT_VISION_MODEL,
                               force=True, reconvert=False)
    except (IngestError, NotImplementedError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)


@cli.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--note", type=str, default=None, help="Free-text hint passed to the converter.")
@click.option("-f", "--force", is_flag=True,
              help="Re-add even if already ingested. Drops the existing entry "
                   "and reruns extract; cached markdown is reused.")
@click.option("--reconvert", is_flag=True,
              help="Also redo PDF→Markdown conversion (drops cached markdown). "
                   "Implies --force.")
@click.option("--no-diagram", is_flag=True,
              help="Skip diagram generation after a successful PDF ingest.")
@click.option("--debug", is_flag=True, help="Log every LLM prompt and response.")
def add(input_path: Path, note: str | None, force: bool, reconvert: bool,
        no_diagram: bool, debug: bool):
    """Ingest a source into the project graph (whole pipeline).

    Convenience wrapper around `convert` + `extract`. Equivalent to running
    them in sequence; cached intermediate artifacts are reused.

    Supported inputs:
      .ttl/.n3  — symlinked into .docgraph/graphs/ and registered (no LLM).
      .pdf      — converted to Markdown (cached), then classified and
                  extracted via the 14-prompt ISO 15926-2 pipeline.

    Pass --debug to log the full prompt and response for every LLM call.
    """
    _setup_logging(debug)
    source = input_path.resolve()
    project_root = _find_project(source)

    suffix = source.suffix.lower()
    try:
        if suffix in TTL_SUFFIXES:
            ingest_ttl(source, project_root, console, force=force or reconvert)
            return

        if suffix == ".pdf":
            client = _anthropic_client()
            _ingest_pdf_dispatched(project_root, source,
                                   client=client, model=DEFAULT_VISION_MODEL,
                                   note=note, force=force, reconvert=reconvert)

            if not no_diagram:
                from src.diagram import DiagramError, make_diagram
                slug = _resolve_slug(project_root, str(source))
                try:
                    make_diagram(project_root, slug, console)
                except DiagramError as exc:
                    console.print(f"  [yellow]diagram skipped[/yellow]: {exc}")
                except Exception as exc:
                    console.print(f"  [yellow]diagram failed[/yellow]: {exc}")
            return
    except (IngestError, NotImplementedError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    console.print(
        f"[yellow]Not yet supported:[/yellow] extraction from {suffix!r} files. "
        "See ARCHITECTURE.md (extraction pipeline)."
    )
    sys.exit(2)


def _resolve_slug(project_root: Path, target: str) -> str:
    """Resolve *target* to a slug. If *target* is an existing file, look it up
    in sources.ttl by absolute path, then by content hash; otherwise treat it
    as a literal slug and verify it's registered.
    """
    sources = list_sources(project_root)
    by_slug = {s["slug"]: s for s in sources}

    p = Path(target)
    if p.exists() and p.is_file():
        absolute = str(p.resolve())
        for s in sources:
            if s["sourcePath"] == absolute:
                return s["slug"]
        # Path didn't match — try hash for moved/renamed files.
        from src.ingest import compute_hash
        file_hash = compute_hash(p.resolve())
        for s in sources:
            if s["fileHash"] == file_hash:
                return s["slug"]
        raise click.UsageError(
            f"{p} is not registered in this project (run `docgraph status` to list sources)."
        )

    # Not a file — treat as slug.
    if target in by_slug:
        return target
    raise click.UsageError(
        f"no source registered as {target!r} (run `docgraph status` to list sources)."
    )


@cli.command()
@click.argument("target", required=False)
@click.option("--all", "all_sources", is_flag=True,
              help="Generate diagrams for every source in the project.")
@click.option("--format", "fmt", type=click.Choice(["svg", "png"]), default="svg",
              show_default=True, help="Render format (best-effort via plantuml.com).")
@click.option("--direction", type=click.Choice(["LR", "TB"]), default="LR",
              show_default=True, help="Diagram layout direction (left-to-right or top-to-bottom).")
def diagram(target: str | None, all_sources: bool, fmt: str, direction: str):
    """Generate a PlantUML diagram from a source's extraction named graph.

    TARGET may be either a slug (e.g. `zahnrechnung-2025`) or a path to the
    original source file (e.g. `~/Documents/Zahnrechnung2025.pdf`); paths are
    resolved against the project's sources.ttl.

    Pipeline:  graphs/<slug>.trig  →  diagrams/<slug>.puml  →  diagrams/<slug>.svg

    The .puml is always written. Rendering is best-effort over the public
    PlantUML server; if the network call fails the .puml is still on disk.
    """
    project_root = find_project_root(Path.cwd())
    if project_root is None:
        console.print("[red]Error:[/red] not a docgraph project (run `docgraph init`).")
        sys.exit(1)

    if all_sources:
        slugs = [s["slug"] for s in list_sources(project_root)]
    elif target:
        try:
            slugs = [_resolve_slug(project_root, target)]
        except click.UsageError as exc:
            console.print(f"[red]Error:[/red] {exc.message}")
            sys.exit(1)
    else:
        console.print("[red]Error:[/red] specify a slug, a file path, or pass --all.")
        sys.exit(1)

    from src.diagram import DiagramError, make_diagram
    for s in slugs:
        console.print(f"[bold]{s}[/bold]")
        try:
            make_diagram(project_root, s, console, render_format=fmt, direction=direction)
        except DiagramError as exc:
            console.print(f"  [red]error:[/red] {exc}")
        except Exception as exc:
            console.print(f"  [red]unexpected error:[/red] {exc}")


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=None, required=False)
def status(directory: Path | None):
    """Show the project's ingested sources."""
    project_root = find_project_root((directory or Path.cwd()).resolve())
    if project_root is None:
        console.print("[red]Error:[/red] not a docgraph project (run `docgraph init`).")
        sys.exit(1)

    sources = list_sources(project_root)
    console.print(f"Project: [dim]{project_root}[/dim]")
    console.print(f"Sources: [bold]{len(sources)}[/bold]\n")
    if not sources:
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Slug")
    table.add_column("Label")
    table.add_column("Mime")
    table.add_column("Size", justify="right")
    table.add_column("Added")
    for s in sources:
        table.add_row(
            s["slug"],
            s["label"],
            s["mimeType"],
            f"{s['fileSize']:,}",
            s["addedAt"][:19],  # trim sub-second / tz tail for the table
        )
    console.print(table)


if __name__ == "__main__":
    cli()
