#!/usr/bin/env python3
"""PDF tax document classifier CLI."""

import logging
import os
import sys
import traceback
from pathlib import Path

import anthropic
import click
from rich.console import Console
from rich.table import Table

from src.classifier.classifier import markdown_content_block
from src.classifier.agent import run_extraction
from src.classifier.markdown_io import load_or_extract
from src.classifier.ontology import (
    load_document_classes,
    load_preferred_model,
    load_self,
)
from src.classifier.results import append_result, find_classified, pdf_sha256
from src.classifier.validator import validate

console = Console()


@click.command()
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option(
    "--output", "-o",
    type=click.Path(path_type=Path),
    default=Path("./classified"),
    show_default=True,
    help="Output directory for organized PDFs.",
)
@click.option(
    "--dry-run", "-n",
    is_flag=True,
    help="Show what would happen without copying files.",
)
@click.option(
    "--min-confidence",
    type=float,
    default=0.5,
    show_default=True,
    help="Skip files with confidence below this threshold.",
)
@click.option(
    "--self-ontology",
    "self_path",
    type=click.Path(exists=True, path_type=Path),
    default=Path(__file__).parent / "data" / "self.ttl",
    show_default=True,
    help="Project registry ontology (self.ttl). Declares all other ontologies.",
)
@click.option(
    "--fetch-remote",
    is_flag=True,
    help="Fetch remote ontologies (FOAF, SKOS, PROV-O) listed in self.ttl.",
)
@click.option(
    "--debug",
    is_flag=True,
    help="Enable debug logging (prints prompts and raw LLM responses).",
)
@click.option(
    "--force", "-f",
    is_flag=True,
    help="Re-classify files even if they have already been processed.",
)
@click.option(
    "--note",
    type=str,
    default=None,
    help="Custom hint passed to the classifier, e.g. 'Contains Invoice and Receipt in top right corner'.",
)
def main(input_path: Path, output: Path, dry_run: bool, min_confidence: float, self_path: Path, fetch_remote: bool, debug: bool, force: bool, note: str | None):
    """
    Classify PDF tax documents and organize them into folders.

    INPUT_PATH can be a single PDF file or a directory of PDFs.
    """
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    if debug:
        logging.getLogger("src.classifier").setLevel(logging.DEBUG)

    # ── Load project registry (self.ttl) ──────────────────────────────────────
    console.print(f"Loading project registry from [dim]{self_path}[/dim]...")
    self_cfg = load_self(self_path, load_remote=fetch_remote)
    console.print(f"  namespaces: {', '.join(self_cfg.namespaces)}")
    console.print(f"  target class: [dim]{self_cfg.target_class}[/dim]\n")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Error:[/red] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    model = load_preferred_model(self_cfg.graph)
    console.print(f"Using model: [bold]{model.label}[/bold] ({model.model_id})\n")

    doc_classes = load_document_classes(self_cfg.graph, self_cfg.target_class)
    console.print(f"Loaded [bold]{len(doc_classes)}[/bold] document classes: {', '.join(doc_classes)}\n")

    client = anthropic.Anthropic(api_key=api_key)

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

    results_ttl = output / "results.ttl"

    if force and results_ttl.exists():
        results_ttl.unlink()
        console.print(f"  [yellow]--force: removed existing {results_ttl}[/yellow]\n")

    # Keep results in memory so find_entity can query across documents in the same run.
    from rdflib import Graph as _Graph
    results_graph = _Graph()
    if results_ttl.exists():
        results_graph.parse(results_ttl)

    for pdf in pdfs:
        console.print(f"[bold]{pdf.name}[/bold]")
        try:
            if not dry_run and not force:
                existing = find_classified(results_ttl, pdf_sha256(pdf))
                if existing:
                    doc_id = str(existing).rsplit("/", 1)[-1]
                    console.print(f"  [dim]already classified as {doc_id}, skipping[/dim]\n")
                    continue

            docs = load_or_extract(pdf, force, client, model, console, note=note)
            content_block = markdown_content_block(docs)

            def _on_classified(hit):
                console.print(f"    [cyan]{hit.category}[/cyan] ({hit.confidence:.0%}): {hit.reason}")

            def _on_extracted(hit):
                if hit.category not in doc_classes:
                    console.print(f"  [yellow]unknown category '{hit.category}' — skipping save[/yellow]")
                    return
                if not dry_run:
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
                client, model, note=note,
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

                if not dry_run:
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

    if dry_run:
        console.print("\n[dim]Dry run — no files were copied.[/dim]")


if __name__ == "__main__":
    main()
