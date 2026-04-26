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
from src.models import ModelConfig
from src.project import (
    UNRESOLVED_FILENAME,
    find_project_root,
    graphs_dir,
    init_project,
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
def init(directory: Path | None, force: bool):
    """Initialise a .docgraph/ project directory (analogous to git init)."""
    target = (directory or Path.cwd()).resolve()
    if not target.is_dir():
        console.print(f"[red]Error:[/red] {target} is not a directory.")
        sys.exit(1)
    try:
        init_project(target, console, force=force)
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
    console.print(f"[green]Cleaned[/green] {len(targets)} graph(s) and reset sources.ttl")


@cli.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--note", type=str, default=None, help="Free-text hint passed to the converter.")
@click.option("-f", "--force", is_flag=True,
              help="Re-add even if already ingested. Drops the existing entry "
                   "and reruns classify + extract; cached markdown is reused.")
@click.option("--reconvert", is_flag=True,
              help="Also redo PDF→Markdown conversion (drops cached markdown). "
                   "Implies --force.")
@click.option("--debug", is_flag=True, help="Log every LLM prompt and response.")
def add(input_path: Path, note: str | None, force: bool, reconvert: bool, debug: bool):
    """Ingest a source into the project graph.

    Supported inputs:
      .ttl/.n3  — symlinked into .docgraph/graphs/ and registered (no LLM).
      .pdf      — converted to Markdown, registered as a lis:InformationObject
                  with full PROV-O provenance, classified against existing
                  subclasses of lis:InformationObject, and instance-extracted
                  for as many properties of the chosen class as the document
                  supports (one level of object-property nesting).

    Pass --debug to log the full prompt and response for every LLM call.
    """
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if debug:
        logging.getLogger("src").setLevel(logging.DEBUG)

    source = input_path.resolve()
    project_root = find_project_root(source.parent)
    if project_root is None:
        project_root = find_project_root(Path.cwd())
    if project_root is None:
        console.print("[red]Error:[/red] not a docgraph project (run `docgraph init`).")
        sys.exit(1)
    console.print(f"Project root: [dim]{project_root}[/dim]")

    suffix = source.suffix.lower()
    try:
        if suffix in TTL_SUFFIXES:
            ingest_ttl(source, project_root, console, force=force or reconvert)
            return

        if suffix == ".pdf":
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                console.print("[red]Error:[/red] ANTHROPIC_API_KEY environment variable not set.")
                sys.exit(1)
            client = AnthropicClient(api_key=api_key)
            ingest_pdf(source, project_root, console,
                       client=client, model=DEFAULT_VISION_MODEL, note=note,
                       force=force, reconvert=reconvert)
            return
    except IngestError as exc:
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
