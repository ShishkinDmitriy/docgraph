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
from src.html_io import (
    html_paths_for_pdf,
    load_or_extract_html,
)
from src.models import ModelConfig
from src.project import (
    DEFAULT_PIPELINE,
    PIPELINE_PART14,
    PIPELINES,
    annotated_dir,
    cache_dir,
    find_project_root,
    graphs_dir,
    html_dir,
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
                     if p.suffix in (".ttl", ".trig"))

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
              help="Re-run conversion even if cached HTML exists.")
@click.option("--debug", is_flag=True, help="Log every LLM prompt and response.")
def convert(input_path: Path, note: str | None, force: bool, debug: bool):
    """Convert a PDF source to canonical HTML and cache the result.

    First stage of the extraction pipeline (see docs/architecture/html-pipeline.md).
    Output lands in `.docgraph/html/<slug>.html` (one file per detected
    document — most PDFs are one doc, but invoice + receipt PDFs split).
    Subsequent `docgraph extract` and `docgraph add` invocations reuse this
    cache; the markdown view consumed by extraction is derived on demand.

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
    h_dir = html_dir(project_root)
    h_dir.mkdir(parents=True, exist_ok=True)
    docs = load_or_extract_html(
        source, force=force, client=client, model=DEFAULT_VISION_MODEL,
        con=console, note=note, html_dir=h_dir,
    )
    console.print(f"  cached [bold]{len(docs)}[/bold] HTML document(s)")


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
                   "and reruns extract; cached HTML is reused.")
@click.option("--reconvert", is_flag=True,
              help="Also redo PDF→HTML conversion (drops cached HTML). "
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
      .pdf      — converted to HTML (canonical, cached at .docgraph/html/),
                  then extracted via the Part 14 root walker. Extraction
                  consumes a markdown view derived on demand from the HTML.

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
@click.argument("target", required=True)
def coverage(target: str):
    """Report which atomic units of the source HTML are cited in the graph.

    For each element with `id="id-N"` in the canonical HTML, check whether
    any graph triple cites `<doc#id-N>` directly OR cites a `<doc#class-N>`
    fragment that covers it. The report shows total coverage, lists
    uncovered units (so you can see what the extractor missed), and breaks
    down by data-note section when those are present.

    Pass either a slug (registered in sources.ttl) or a path to the source.
    """
    from src.coverage import coverage_for_files

    project_root = _find_project(Path.cwd())
    slug = _resolve_slug(project_root, target)

    # HTML file naming preserves the PDF stem's case; the slug is lowercased.
    # Find a file whose name (case-insensitive) matches the slug.
    h_dir = html_dir(project_root)
    html_path: Path | None = None
    if h_dir.exists():
        for p in h_dir.glob("*.html"):
            if p.stem.casefold() == slug.casefold():
                html_path = p
                break
    graph_path = graphs_dir(project_root) / f"{slug}.extract.ttl"

    if html_path is None:
        console.print(f"[red]Error:[/red] no HTML file matching slug {slug!r} "
                      f"in {h_dir.relative_to(project_root)}/")
        sys.exit(1)
    if not graph_path.exists():
        console.print(f"[red]Error:[/red] {graph_path} not found.")
        sys.exit(1)

    report = coverage_for_files(html_path, graph_path)

    # Headline
    console.print(f"\n[bold]Coverage[/bold]  {slug}")
    console.print(f"  HTML:  [dim]{html_path.relative_to(project_root)}[/dim]")
    console.print(f"  Graph: [dim]{graph_path.relative_to(project_root)}[/dim]\n")

    if report.total == 0:
        console.print("  [yellow]No atomic units found in HTML.[/yellow]")
        return

    pct_color = "green" if report.percent >= 80 else ("yellow" if report.percent >= 50 else "red")
    console.print(
        f"  Atomic units cited: "
        f"[bold]{report.covered}[/bold] / [bold]{report.total}[/bold]  "
        f"[{pct_color}]({report.percent:.0f}%)[/{pct_color}]"
    )
    n_class_cites = sum(1 for c in report.citations if c.startswith("class-"))
    n_id_cites    = sum(1 for c in report.citations if c.startswith("id-"))
    console.print(
        f"  Citation fragments: [bold]{n_id_cites}[/bold] id-N, "
        f"[bold]{n_class_cites}[/bold] class-N\n"
    )

    # Uncovered list
    uncovered = report.uncovered
    if uncovered:
        from rich.markup import escape as _esc
        console.print(f"[bold]Uncovered atomic units[/bold]  ({len(uncovered)})")
        for u in uncovered:
            section = f"  [dim]({_esc(u.section)})[/dim]" if u.section else ""
            text = _esc(u.text) if u.text else "[dim](empty)[/dim]"
            cls = f"  [dim].{u.css_class}[/dim]" if u.css_class else ""
            console.print(f"  #[bold]{u.id_}[/bold] <{u.tag}> {text}{cls}{section}")
        console.print()

    # Per-section breakdown
    sections: dict[str | None, list[int]] = {}   # section → [covered, total]
    for u in report.units:
        sec_bucket = sections.setdefault(u.section, [0, 0])
        sec_bucket[1] += 1
        if u.id_ in report.covered_ids:
            sec_bucket[0] += 1
    if any(s for s in sections if s):
        from rich.markup import escape as _esc
        console.print("[bold]By section[/bold]")
        for sec, (cov, tot) in sorted(sections.items(), key=lambda kv: (kv[0] or "")):
            label = _esc(sec) if sec else "[dim](no enclosing data-note)[/dim]"
            color = "green" if cov == tot else ("yellow" if cov > 0 else "red")
            console.print(f"  [{color}]{cov}/{tot}[/{color}]  {label}")


@cli.command()
@click.argument("target", required=True)
@click.option("--no-open", is_flag=True,
              help="Generate the annotated HTML but don't open it in a browser.")
def view(target: str, no_open: bool):
    """Open an annotated HTML view of the document showing extracted entities.

    Generates `.docgraph/annotated/<slug>.html` from the canonical HTML +
    extract graph: every cited element gets a green highlight + entity
    label badge; uncovered atomic units stay outlined dashed-red. Hover any
    cited element to see its URI / types / label; the floating sidebar
    lists all entities with click-to-jump.

    The annotated view is fully derived — regenerable any time, never the
    source of truth. Pass --no-open to skip the browser launch.
    """
    from src.annotated_view import render_annotated_view

    project_root = _find_project(Path.cwd())
    slug = _resolve_slug(project_root, target)

    h_dir = html_dir(project_root)
    html_path: Path | None = None
    if h_dir.exists():
        for p in h_dir.glob("*.html"):
            if p.stem.casefold() == slug.casefold():
                html_path = p
                break
    graph_path = graphs_dir(project_root) / f"{slug}.extract.ttl"

    if html_path is None:
        console.print(f"[red]Error:[/red] no HTML file matching slug {slug!r}.")
        sys.exit(1)
    if not graph_path.exists():
        console.print(f"[red]Error:[/red] {graph_path} not found.")
        sys.exit(1)

    out_dir = annotated_dir(project_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{slug}.html"

    annotated = render_annotated_view(html_path, graph_path, title=slug)
    out_path.write_text(annotated, encoding="utf-8")
    console.print(f"  wrote   [dim]{out_path.relative_to(project_root)}[/dim]")

    if not no_open:
        import webbrowser
        webbrowser.open(out_path.as_uri())


# ── Delta-history CLI (versioned named graphs) ────────────────────────────


@cli.command()
@click.argument("target", required=True)
def history(target: str):
    """List the version history of a doc's graph deltas.

    For every `<scope-prefix>.NNN.trig` delta file in the doc's scope,
    prints: seq, step, +N/-M triple counts, agent (if recorded), timestamp.
    Pass either a slug (registered in sources.ttl) or a path to the source.
    """
    from src.deltas import doc_scope, list_deltas_for_scope, read_delta

    project_root = _find_project(Path.cwd())
    slug = _resolve_slug(project_root, target)
    g_dir = graphs_dir(project_root)

    paths = list_deltas_for_scope(g_dir, doc_scope(slug))
    if not paths:
        console.print(f"[yellow]No delta files for[/yellow] [bold]{slug}[/bold].")
        console.print(f"  (Looked under {g_dir.relative_to(project_root)}/doc-{slug}.NNN.trig)")
        return

    console.print(f"\n[bold]History[/bold]  {slug}\n")
    for path in paths:
        try:
            delta = read_delta(path)
        except ValueError as exc:
            console.print(f"  [red]seq=? — {path.name}: {exc}[/red]")
            continue
        added_n   = len(delta.added)
        removed_n = len(delta.removed)
        added_str   = f"[green]+{added_n}[/green]"   if added_n   else "[dim]+0[/dim]"
        removed_str = f"[red]-{removed_n}[/red]"     if removed_n else "[dim]-0[/dim]"
        agent_str   = (f"  [dim]agent: {delta.agent}[/dim]" if delta.agent else "")
        ts_str      = (f"  [dim]{delta.timestamp.isoformat()}[/dim]" if delta.timestamp else "")
        console.print(f"  [bold]seq {delta.seq:>3}[/bold]  "
                      f"{delta.step:<12} {added_str:>15} {removed_str:>15}"
                      f"{ts_str}{agent_str}")
    console.print()


@cli.command()
@click.argument("target", required=True)
@click.argument("seq_a", type=int, required=True)
@click.argument("seq_b", type=int, required=True)
def diff(target: str, seq_a: int, seq_b: int):
    """Show the composed (added, removed) diff between two seqs of a doc.

    `materialize(at_seq=seq_b)` minus `materialize(at_seq=seq_a)` —
    i.e. what triples got added/removed by the steps with seq in (seq_a, seq_b].
    Useful to see "what did the dedup phase actually do" or "what did seqs
    2..3 contribute" without grepping individual delta files.
    """
    from src.deltas import doc_scope, materialize

    project_root = _find_project(Path.cwd())
    slug = _resolve_slug(project_root, target)
    g_dir = graphs_dir(project_root)
    scope = doc_scope(slug)

    state_a = materialize(g_dir, scope, at_seq=seq_a)
    state_b = materialize(g_dir, scope, at_seq=seq_b)
    a_set = set(state_a)
    b_set = set(state_b)
    added   = b_set - a_set
    removed = a_set - b_set

    console.print(f"\n[bold]Diff[/bold]  {slug}  "
                  f"seq {seq_a} → {seq_b}\n")
    console.print(f"  Added:   [green]+{len(added)}[/green] triples")
    console.print(f"  Removed: [red]-{len(removed)}[/red] triples\n")

    if added:
        console.print("[bold green]+ Added[/bold green]")
        for triple in sorted(added, key=str)[:50]:
            console.print(f"  [green]+[/green]  {triple[0]}  {triple[1]}  {triple[2]}")
        if len(added) > 50:
            console.print(f"  [dim]…and {len(added) - 50} more[/dim]")
        console.print()
    if removed:
        console.print("[bold red]- Removed[/bold red]")
        for triple in sorted(removed, key=str)[:50]:
            console.print(f"  [red]-[/red]  {triple[0]}  {triple[1]}  {triple[2]}")
        if len(removed) > 50:
            console.print(f"  [dim]…and {len(removed) - 50} more[/dim]")
        console.print()


@cli.command()
@click.argument("target", required=True)
@click.option("--at", "at_seq", type=int, default=None,
              help="Materialize state at this seq (default: HEAD).")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Output .ttl path (default: <slug>.<seq>.snapshot.ttl OR "
                   "<slug>.HEAD.snapshot.ttl in graphs/).")
def snapshot(target: str, at_seq: int | None, out_path: Path | None):
    """Write a materialized snapshot (full Turtle) of a doc's scope.

    Default writes HEAD (all deltas applied) to
    `<graphs>/<slug>.HEAD.snapshot.ttl`. With `--at <seq>`, writes the
    historical state after the step with that seq to
    `<graphs>/<slug>.NNN.snapshot.ttl`.
    """
    from src.deltas import doc_scope, materialize

    project_root = _find_project(Path.cwd())
    slug = _resolve_slug(project_root, target)
    g_dir = graphs_dir(project_root)
    scope = doc_scope(slug)

    g = materialize(g_dir, scope, at_seq=at_seq)
    if len(g) == 0:
        console.print(f"[yellow]No triples to write[/yellow] for {slug}"
                      f"{f' at seq={at_seq}' if at_seq is not None else ''}.")
        return

    if at_seq is not None:
        default_name = f"{slug}.{at_seq:03d}.snapshot.ttl"
    else:
        default_name = f"{slug}.HEAD.snapshot.ttl"
    if out_path is None:
        out_path = g_dir / default_name
    out_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(out_path), format="turtle")
    console.print(f"  wrote   [dim]{out_path}[/dim] ({len(g)} triples)")


@cli.command()
@click.option("--threshold", "threshold", type=int, default=2,
              help="Minimum number of docs that must declare a class for it to "
                   "promote (default 2).")
@click.option("--dry-run", is_flag=True,
              help="Show what would be promoted without writing any deltas.")
def promote(threshold: int, dry_run: bool):
    """Promote stable ext: classes from per-doc graphs to project scope.

    Scans every doc-scope graph for ext: class declarations. Classes
    declared in ≥threshold docs are merged into a canonical definition
    written to project scope. Each contributing doc's scope gets a
    `promote` delta that REMOVES the class declaration (the canonical
    URI continues to be valid; instances in those docs keep working).

    Pure mechanical, no LLM. The dedup phase did the LLM-aided semantic
    matching upstream; promote just consolidates the result into a
    cross-doc ontology layer.
    """
    from src.extract_part14.promote import walk_promote

    project_root = _find_project(Path.cwd())
    g_dir = graphs_dir(project_root)

    if dry_run:
        # Build the decisions without writing — caller wants a preview
        from src.extract_part14.promote import PromotionDecision
        from src.deltas import list_scopes, materialize, project_scope
        from src.extract_part14.ext_ontology import extract_classes_from_graph
        from collections import defaultdict
        contributors_by_slug: dict[str, list[str]] = defaultdict(list)
        project_state = materialize(g_dir, project_scope())
        already_promoted = set(extract_classes_from_graph(project_state).keys())
        for scope in list_scopes(g_dir):
            if scope.kind != "doc" or not scope.name:
                continue
            per_doc_classes = extract_classes_from_graph(materialize(g_dir, scope))
            for slug in per_doc_classes:
                if slug in already_promoted:
                    continue
                contributors_by_slug[slug].append(scope.name)
        candidates = [(slug, contribs) for slug, contribs in contributors_by_slug.items()
                       if len(contribs) >= threshold]
        if not candidates:
            console.print(f"[yellow]No ext class meets threshold ≥{threshold} docs.[/yellow]")
            return
        console.print(f"[bold]Would promote {len(candidates)} class(es)[/bold] "
                      f"(threshold ≥{threshold} docs):\n")
        for slug, contribs in sorted(candidates):
            console.print(f"  ext:[bold]{slug}[/bold]   "
                          f"{len(contribs)} contributors: {', '.join(contribs)}")
        console.print()
        return

    console.print(f"[bold]promote[/bold]   (threshold ≥{threshold} docs)")
    decisions = walk_promote(g_dir, threshold=threshold, console=console)
    if not decisions:
        return
    console.print(f"  → promoted {len(decisions)} class(es) to project scope")


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
