#!/usr/bin/env python3
"""PDF tax document classifier CLI."""

import json
import logging
import os
import sys
from pathlib import Path

import anthropic
import click
from rich.console import Console
from rich.table import Table

from src.classifier.extractor import extract_text, extract_images
from src.classifier.classifier import classify, classify_from_images, extract_details
from src.classifier.ontology import (
    build_category_descriptions,
    build_extraction_prompt,
    load_document_classes,
    load_class_properties,
    load_preferred_model,
)
from src.classifier.interactive import fill_missing
from src.classifier.results import append_result, find_classified, pdf_sha256
from src.classifier.validator import validate, ShapeViolation

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
    "--categories",
    "categories_path",
    type=click.Path(exists=True, path_type=Path),
    default=Path(__file__).parent / "data" / "financial_documents.ttl",
    show_default=True,
    help="RDF file (Turtle) defining document categories.",
)
@click.option(
    "--models",
    "models_path",
    type=click.Path(exists=True, path_type=Path),
    default=Path(__file__).parent / "data" / "models.ttl",
    show_default=True,
    help="RDF file (Turtle) defining available LLM models.",
)
@click.option(
    "--shapes",
    "shapes_path",
    type=click.Path(path_type=Path),
    default=Path(__file__).parent / "data" / "shapes.ttl",
    show_default=True,
    help="SHACL shapes file for validating classification results.",
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
def main(input_path: Path, output: Path, dry_run: bool, min_confidence: float, categories_path: Path, models_path: Path, shapes_path: Path, debug: bool, force: bool):
    """
    Classify PDF tax documents and organize them into folders.

    INPUT_PATH can be a single PDF file or a directory of PDFs.
    """
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s | %(message)s")
        logging.getLogger("anthropic").setLevel(logging.WARNING)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Error:[/red] ANTHROPIC_API_KEY environment variable not set.")
        sys.exit(1)

    console.print(f"Loading models from [dim]{models_path}[/dim]...")
    model = load_preferred_model(models_path)
    console.print(f"Using model: [bold]{model.label}[/bold] ({model.model_id})\n")

    console.print(f"Loading categories from [dim]{categories_path}[/dim]...")
    doc_classes   = load_document_classes(categories_path)
    class_props   = load_class_properties(categories_path)
    categories    = build_category_descriptions(doc_classes, class_props)
    with_props    = sum(1 for p in class_props.values() if p)
    console.print(f"Loaded [bold]{len(categories)}[/bold] categories ({with_props} with properties): {', '.join(categories)}\n")

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

    for pdf in pdfs:
        console.print(f"[bold]{pdf.name}[/bold]")
        try:
            if not dry_run and not force:
                existing = find_classified(results_ttl, pdf_sha256(pdf))
                if existing:
                    doc_id = str(existing).rsplit("/", 1)[-1]
                    console.print(f"  [dim]already classified as {doc_id}, skipping[/dim]\n")
                    continue

            console.print(f"  extracting text...")
            text = extract_text(pdf)

            if not text.strip():
                console.print(f"  [dim]no text layer — rendering pages for vision OCR...[/dim]")
                images = extract_images(pdf)
                if not images:
                    console.print(f"  [red]failed:[/red] could not render any pages")
                    continue
                for img in images:
                    console.print(f"  saved page → {img['_path']}")
                console.print(f"  sending {len(images)} page(s) to Claude vision...")
                result, messages = classify_from_images(images, client, categories, model)
                method = "vision"
            else:
                console.print(f"  extracted {len(text)} chars, classifying...")
                result, messages = classify(text, client, categories, model)
                method = "text"

            hits = result.documents
            console.print(f"  detected [bold]{len(hits)}[/bold] document type(s):")
            for hit in hits:
                console.print(f"    [cyan]{hit.category}[/cyan] ({hit.confidence:.0%}): {hit.reason}")

            accepted_hits = [h for h in hits if h.confidence >= min_confidence]
            skipped = len(hits) - len(accepted_hits)
            if skipped:
                console.print(f"  [yellow]skipping {skipped} hit(s) below confidence threshold {min_confidence:.0%}[/yellow]")

            for hit in accepted_hits:
                if hit.category not in doc_classes:
                    console.print(f"  [yellow]unknown category '{hit.category}' — skipping[/yellow]")
                    continue

                props = class_props.get(hit.category, [])
                if props:
                    console.print(f"  [{hit.category}] extracting {len(props)} properties...")
                    try:
                        hit.details = extract_details(
                            messages, build_extraction_prompt(props), client, model
                        )
                        console.print(f"  [{hit.category}] details: {json.dumps(hit.details, indent=4)}")
                    except Exception as detail_err:
                        console.print(f"  [{hit.category}] [yellow]detail extraction failed:[/yellow] {detail_err}")

                if not dry_run:
                    append_result(
                        results_ttl, pdf, hit, result,
                        model=model, method=method,
                        doc_class=doc_classes[hit.category],
                        class_props=props,
                    )
                    console.print(f"  [{hit.category}] saved to RDF → {results_ttl}")

                    if shapes_path.exists():
                        violations = validate(results_ttl, shapes_path)
                        if violations:
                            console.print(f"  [yellow]SHACL[/yellow] {len(violations)} violation(s):")
                            for v in violations:
                                node = v.focus_node.split("/")[-1]
                                path = v.result_path.split("#")[-1] if v.result_path else "—"
                                console.print(f"    [{('red' if v.severity == 'violation' else 'yellow')}]{v.severity}[/] {node}  {path}  {v.message}")

                            if any(v.is_missing_field for v in violations):
                                console.print(f"  [{hit.category}] attempting to fill missing fields…")
                                hit = fill_missing(
                                    violations, hit,
                                    class_props=props,
                                    messages=messages,
                                    client=client,
                                    model=model,
                                )
                                append_result(
                                    results_ttl, pdf, hit, result,
                                    model=model, method=method,
                                    doc_class=doc_classes[hit.category],
                                    class_props=props,
                                )
                                second_pass = validate(results_ttl, shapes_path)
                                if second_pass:
                                    console.print(f"  [yellow]SHACL[/yellow] still {len(second_pass)} violation(s) after fill")
                                else:
                                    console.print(f"  [green]SHACL[/green] conforms after fill")
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
            import traceback
            console.print(f"  [red]error:[/red] {e}")
            console.print(f"  [dim]{traceback.format_exc().strip()}[/dim]\n")

    console.print(table)

    if dry_run:
        console.print("\n[dim]Dry run — no files were copied.[/dim]")


if __name__ == "__main__":
    main()
