#!/usr/bin/env python3
"""PDF tax document classifier CLI."""

import logging
import os
import sys
import traceback
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from src.classifier import markdown_content_block
from src.agent import run_extraction
from src.markdown_io import load_or_extract
from src.ontology import (
    load_document_classes,
    load_preferred_model,
    load_vision_model,
    load_docgraph,
)
from src.llm.anthropic import AnthropicClient
from src.llm.openai import OpenAIClient
from src.project import (
    find_project_root,
    init_project,
    registry_path,
    cache_dir as project_cache_dir,
)
from src.results import append_result, find_classified, pdf_sha256
from src.validator import validate

console = Console()


@click.group()
def cli():
    """Classify PDF documents and extract structured RDF data using Claude."""


@cli.command()
@click.argument("directory", type=click.Path(path_type=Path), default=None, required=False)
@click.option(
    "--force", "-f",
    is_flag=True,
    help="Reinitialise even if .docgraph/ already exists.",
)
def init(directory: Path | None, force: bool):
    """
    Initialise a .docgraph/ project directory.

    Creates .docgraph/ in DIRECTORY (default: current working directory) with a
    registry, default ontology files, and an empty cache.  Analogous to git init.
    """
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
@click.option(
    "--docgraph",
    "docgraph_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help="Project registry (docgraph.ttl). Auto-discovered when omitted.",
)
@click.option(
    "--yes", "-y",
    is_flag=True,
    help="Skip confirmation prompt.",
)
def clean(directory: Path | None, docgraph_path: Path | None, yes: bool):
    """
    Remove the extracted results file (results.ttl).

    Leaves the Markdown cache and ontology files untouched.
    Run 'docgraph add' afterwards to re-extract from scratch.
    """
    if docgraph_path is None:
        start = (directory or Path.cwd()).resolve()
        project_root = find_project_root(start)
        if project_root is not None:
            docgraph_path = registry_path(project_root)
        else:
            docgraph_path = Path(__file__).parent / "data" / "docgraph.ttl"

    self_cfg = load_docgraph(docgraph_path, load_remote=False)
    results_ttl = self_cfg.output_path

    if not results_ttl.exists():
        console.print(f"[dim]Nothing to clean — {results_ttl} does not exist.[/dim]")
        return

    if not yes:
        click.confirm(f"Remove {results_ttl}?", abort=True)

    results_ttl.unlink()
    console.print(f"[green]Removed[/green] {results_ttl}")


@cli.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--min-confidence",
    type=float,
    default=0.5,
    show_default=True,
    help="Skip hits below this threshold.",
)
@click.option(
    "--docgraph",
    "docgraph_path",
    type=click.Path(exists=True, path_type=Path),
    default=None,
    help=(
        "Project registry ontology (docgraph.ttl).  Auto-discovered from "
        ".docgraph/ when omitted."
    ),
)
@click.option(
    "--offline",
    is_flag=True,
    help="Skip fetching remote ontologies listed in docgraph.ttl.",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging (prints prompts and raw LLM responses).",
)
@click.option(
    "--force", "-f",
    is_flag=True,
    help="Re-classify already-processed files (ignores the skip-if-seen check).",
)
@click.option(
    "--note",
    type=str,
    default=None,
    help="Free-text hint passed to the classifier.",
)
def add(
    input_path: Path,
    min_confidence: float,
    docgraph_path: Path | None,
    offline: bool,
    debug: bool,
    force: bool,
    note: str | None,
):
    """
    Classify PDF documents and extract structured data.

    INPUT_PATH can be a single PDF file or a directory of PDFs.
    Output path is configured in docgraph.ttl via docgraph:results.
    Use 'docgraph clean' to wipe extracted results.
    """
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    if debug:
        logging.getLogger("src").setLevel(logging.DEBUG)

    # ── Resolve project registry ──────────────────────────────────────────────
    md_cache_dir: Path | None = None

    if docgraph_path is None:
        project_root = find_project_root(input_path if input_path.is_dir() else input_path.parent)
        if project_root is not None:
            docgraph_path = registry_path(project_root)
            md_cache_dir  = project_cache_dir(project_root)
            console.print(f"Discovered project at [dim]{project_root}[/dim]")
        else:
            # Legacy fallback: data/docgraph.ttl next to main.py
            docgraph_path = Path(__file__).parent / "data" / "docgraph.ttl"

    # ── Load project registry ─────────────────────────────────────────────────
    console.print(f"Loading project registry from [dim]{docgraph_path}[/dim]...")
    self_cfg = load_docgraph(docgraph_path, load_remote=not offline)
    console.print(f"  namespaces: {', '.join(self_cfg.namespaces)}")
    console.print(f"  target class: [dim]{self_cfg.target_class}[/dim]\n")

    model        = load_preferred_model(self_cfg.graph)
    vision_model = load_vision_model(self_cfg.graph) or model
    console.print(f"Extraction model: [bold]{model.label}[/bold] ({model.model_id}) via [dim]{model.provider}[/dim]")
    if vision_model is not model:
        console.print(f"Vision model:     [bold]{vision_model.label}[/bold] ({vision_model.model_id}) via [dim]{vision_model.provider}[/dim]")
    console.print()

    doc_classes = load_document_classes(self_cfg.graph, self_cfg.target_class)
    console.print(f"Loaded [bold]{len(doc_classes)}[/bold] document classes: {', '.join(doc_classes)}\n")

    def _make_client(m):
        if m.provider == "openai":
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                console.print("[red]Error:[/red] OPENAI_API_KEY environment variable not set.")
                sys.exit(1)
            return OpenAIClient(api_key=key)
        else:
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                console.print("[red]Error:[/red] ANTHROPIC_API_KEY environment variable not set.")
                sys.exit(1)
            return AnthropicClient(api_key=key)

    client        = _make_client(model)
    vision_client = _make_client(vision_model) if vision_model is not model else client

    # Collect PDFs
    if input_path.is_file():
        pdfs = [input_path]
    else:
        pdfs = sorted(input_path.rglob("*.pdf"))

    if not pdfs:
        console.print("[yellow]No PDF files found.[/yellow]")
        return

    console.print(f"Found [bold]{len(pdfs)}[/bold] PDF(s). Processing...\n")

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("File", style="dim", max_width=40)
    table.add_column("Detected types (confidence)")

    results_ttl = self_cfg.output_path

    # Keep results in memory so find_entity can query across documents in the same run.
    from rdflib import Graph as _Graph
    results_graph = _Graph()
    if results_ttl.exists():
        results_graph.parse(results_ttl)

    for pdf in pdfs:
        console.print(f"[bold]{pdf.name}[/bold]")
        try:
            if not force:
                existing = find_classified(results_ttl, pdf_sha256(pdf))
                if existing:
                    doc_id = str(existing).rsplit("/", 1)[-1]
                    console.print(f"  [dim]already classified as {doc_id}, skipping[/dim]\n")
                    continue

            docs = load_or_extract(pdf, force, vision_client, vision_model, console, note=note, cache_dir=md_cache_dir)
            content_block = markdown_content_block(docs)

            def _on_classified(hit):
                console.print(f"    [cyan]{hit.category}[/cyan] ({hit.confidence:.0%}): {hit.reason}")

            def _on_extracted(hit):
                if hit.category not in doc_classes:
                    console.print(f"  [yellow]unknown category '{hit.category}' — skipping save[/yellow]")
                    return
                append_result(
                    results_ttl, pdf, hit,
                    model=model, method="agent",
                    doc_class_uri=doc_classes[hit.category].uri,
                )
                # Sync results_graph so subsequent PDFs can find_entity against it.
                results_graph.parse(results_ttl)
                console.print(f"  [{hit.category}] extraction complete → {results_ttl}")

            console.print("  running agent extraction...")
            result = run_extraction(
                content_block, self_cfg.graph, results_graph, doc_classes,
                self_cfg.target_class,
                client, model,
                note=note,
                on_hit_classified=_on_classified,
                on_hit_extracted=_on_extracted,
            )

            hits = result.documents
            console.print(f"  detected [bold]{len(hits)}[/bold] document type(s)")
            accepted_hits = [h for h in hits if h.confidence >= min_confidence]
            skipped = len(hits) - len(accepted_hits)
            if skipped:
                console.print(f"  [yellow]skipping {skipped} hit(s) below confidence threshold {min_confidence:.0%}[/yellow]")

            for hit in accepted_hits:
                if hit.category not in doc_classes:
                    continue  # already warned in _on_extracted

                violations = validate(results_ttl, self_cfg.graph)
                if violations:
                    console.print(f"  [yellow]SHACL[/yellow] {len(violations)} violation(s):")
                    for v in violations:
                        node = v.focus_node.split("/")[-1]
                        path = v.result_path.split("#")[-1] if v.result_path else "—"
                        console.print(f"    [{('red' if v.severity == 'violation' else 'yellow')}]{v.severity}[/] {node}  {path}  {v.message}")
                else:
                    console.print(f"  [green]SHACL[/green] conforms")

            if accepted_hits:
                cats_str = ", ".join(
                    f"[{'green' if h.confidence >= 0.8 else 'yellow'}]{h.category}[/] {h.confidence:.0%}"
                    for h in accepted_hits
                )
                table.add_row(
                    pdf.name,
                    cats_str,
                    "",
                )
            console.print(f"  [green]done[/green]\n")

        except Exception as e:
            console.print(f"  [red]error:[/red] {e}")
            console.print(f"  [dim]{traceback.format_exc().strip()}[/dim]\n")

    console.print(table)


if __name__ == "__main__":
    cli()
