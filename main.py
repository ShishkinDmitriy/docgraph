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

from src.sources import IngestError, list_sources
from src.ttl_ingest import TTL_SUFFIXES, ingest_ttl
from src.llm.anthropic import AnthropicClient
from src.html_io import load_or_extract_html
from src.models import ModelConfig
from src.project import (
    cache_dir,
    find_project_root,
    graphs_dir,
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

# Sentinel for `-f` / `--force` used without a task argument: the
# CLI body replaces it with the command's target task. So
# `dg convert foo.pdf --force` ≡ `dg convert foo.pdf --force convert`.
_FORCE_CURRENT = "_CURRENT_"


def _expand_current(force_tasks: tuple[str, ...], target: str) -> tuple[str, ...]:
    """Replace the _CURRENT_ sentinel (from `--force` without a value)
    with the command's target task name."""
    return tuple(target if f == _FORCE_CURRENT else f for f in force_tasks)


def _force_option(target: str):
    """Click decorator for the standard `-f`/`--force` option. With no
    value, forces the command's target task."""
    return click.option(
        "-f", "--force", "force_tasks", multiple=True,
        is_flag=False, flag_value=_FORCE_CURRENT, metavar="[TASK]",
        help=("Force a task to run even if its dirty check says clean. "
              f"With no value, forces this command's task (`-f` ≡ `-f {target}`). "
              "Repeatable."),
    )


def _run_task(target: str, ctx: dict, *,
              exclude: tuple[str, ...] = (),
              force_tasks: tuple[str, ...] = (),
              error_types: tuple[type, ...] = (IngestError, NotImplementedError),
              ) -> None:
    """Generic CLI → task-DAG runner.

    Expands the `--force` _CURRENT_ sentinel against *target*, stuffs
    `forced_tasks` into ctx, runs the task, catches user-facing errors,
    and prints the doc slug if the pipeline produced one.
    """
    from src.tasks import docgraph
    forced = set(_expand_current(force_tasks, target))
    ctx["forced_tasks"] = forced
    try:
        docgraph.run(target, ctx, console=console,
                     exclude=tuple(exclude), force=forced)
    except error_types as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)
    if "slug" in ctx:
        console.print(f"[dim]doc:[/dim] [bold]{ctx['slug']}[/bold]")


@click.group()
def cli():
    """Build a knowledge graph from documents using ISO 15926 Part 14."""


@cli.command()
@click.argument("path", type=click.Path(path_type=Path),
                default=Path("."), required=False)
@_force_option("init")
def init(path: Path, force_tasks: tuple[str, ...]):
    """Initialise a .docgraph/ project directory (analogous to git init).

    Idempotent — if .docgraph/ already exists, the `init` task's dirty
    check returns False and nothing happens. Use --force to reinit.
    """
    _run_task("init", {
        "path":    path.resolve(),
        "console": console,
    },
    force_tasks=force_tasks,
    error_types=(IngestError, FileExistsError, NotADirectoryError))


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

    # Per-doc dirs under docs/<slug>/ + legacy flat graphs/*.ttl files.
    from src.project import DOCGRAPH_DIR, DOCS_SUBDIR
    targets: list[Path] = []
    docs_root = project_root / DOCGRAPH_DIR / DOCS_SUBDIR
    if docs_root.is_dir():
        targets.extend(sorted(p for p in docs_root.iterdir() if p.is_dir()))
    legacy = graphs_dir(project_root)
    if legacy.is_dir():
        targets.extend(sorted(p for p in legacy.iterdir()
                              if p.suffix in (".ttl", ".trig")))

    if not targets:
        console.print("[dim]Nothing to clean.[/dim]")
        return

    console.print(f"Will remove [bold]{len(targets)}[/bold] ingested graph(s):")
    for p in targets:
        console.print(f"  [dim]{p.relative_to(project_root)}[/dim]")

    if not yes:
        click.confirm("Proceed?", abort=True)

    import shutil
    for p in targets:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

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


# ── Per-doc task CLI commands (auto-generated) ────────────────────────
#
# `dg <task> <pdf>` runs the named task and its dependencies via the
# task registry. All commands share the same flag set (--note, -x, -f,
# --debug) so a user who knows one knows them all.
#
# The CLI stuffs `ctx["path"]` and configuration into ctx; identity
# resolves the project root and validates the input itself.

_DOC_TASK_TARGETS = (
    "recognize", "convert", "extract", "templates", "align",
    # diagram is hand-written (accepts slug-or-path, has --all flag).
)


def _add_doc_task_command(target_name: str) -> None:
    """Register `dg <target_name>` as a CLI command that runs the named
    task (and its deps) via the task registry."""
    @cli.command(name=target_name,
                 help=f"Run the '{target_name}' task and its dependencies "
                      f"for a PDF source. Idempotent — only dirty tasks "
                      f"actually do work.")
    @click.argument("input_path", type=click.Path(exists=True, path_type=Path))
    @click.option("--note", type=str, default=None,
                  help="Free-text hint passed to the converter LLM.")
    @click.option("-x", "--exclude", multiple=True, metavar="TASK",
                  help="Skip this task. Repeatable.")
    @_force_option(target_name)
    @click.option("--debug", is_flag=True,
                  help="Log every LLM prompt and response.")
    def _cmd(input_path, note, exclude, force_tasks, debug):
        _setup_logging(debug)
        _run_task(target_name, {
            "path":    input_path.resolve(),
            "console": console,
            "client":  _anthropic_client(),
            "model":   DEFAULT_VISION_MODEL,
            "note":    note,
        }, exclude=exclude, force_tasks=force_tasks)
    return _cmd


for _name in _DOC_TASK_TARGETS:
    _add_doc_task_command(_name)


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
@click.option("--note", type=str, default=None,
              help="Free-text hint passed to the converter LLM.")
@click.option("-x", "--exclude", multiple=True, metavar="TASK",
              help="Skip this task. Repeatable.")
@_force_option("add")
@click.option("--debug", is_flag=True,
              help="Log every LLM prompt and response.")
def add(input_path: Path, note: str | None,
        exclude: tuple[str, ...], force_tasks: tuple[str, ...], debug: bool):
    """Ingest a source into the project graph (full pipeline).

    Same shape as `dg recognize` / `dg convert` / `dg extract` / etc.
    — it just targets the `add` composite task, which depends on
    every other per-doc task. Idempotent re-runs no-op cleanly.

    Special-cases TTL inputs (.ttl/.n3): symlinks them into
    `.docgraph/graphs/` and registers in sources.ttl. No task DAG
    involved — TTL is its own one-shot pipeline.
    """
    _setup_logging(debug)
    source = input_path.resolve()

    if source.suffix.lower() in TTL_SUFFIXES:
        try:
            ingest_ttl(source, _find_project(source), console,
                       force=bool(force_tasks))
        except (IngestError, NotImplementedError) as exc:
            console.print(f"[red]Error:[/red] {exc}")
            sys.exit(1)
        return

    _run_task("add", {
        "path":    source,
        "console": console,
        "client":  _anthropic_client(),
        "model":   DEFAULT_VISION_MODEL,
        "note":    note,
    }, exclude=exclude, force_tasks=force_tasks)


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
        from src.sources import compute_hash
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

    Pipeline:
      docs/<slug>/delta.*.trig  →  docs/<slug>/diagram.puml  →  diagram.svg

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

    # HTML lives inside the per-doc dir as `docs/<slug>/converted.html`
    # (single-doc PDFs) or `docs/<slug>/converted.<part>.html` (multi-doc).
    from src.html_io import html_paths
    from src.project import doc_dir as _doc_dir
    from src.deltas import doc_scope, materialize
    sd = _doc_dir(project_root, slug)
    found = html_paths(sd) if sd.exists() else []
    html_path: Path | None = found[0] if found else None

    # Materialize the extract graph from the doc-scope deltas to a temp
    # file the coverage analyser can read.
    import tempfile
    g = materialize(project_root, doc_scope(slug))
    if html_path is None or len(g) == 0:
        console.print(f"[red]Error:[/red] {slug!r} not found (HTML or graph missing).")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ttl", delete=False) as tf:
        graph_path = Path(tf.name)
    g.serialize(destination=str(graph_path), format="turtle")

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

    from src.project import (
        annotated_html_path as _ann_path,
        doc_dir as _doc_dir,
    )
    from src.html_io import html_paths
    from src.deltas import doc_scope, materialize
    sd = _doc_dir(project_root, slug)
    found = html_paths(sd) if sd.exists() else []
    html_path: Path | None = found[0] if found else None

    import tempfile
    g = materialize(project_root, doc_scope(slug))
    if html_path is None or len(g) == 0:
        console.print(f"[red]Error:[/red] {slug!r} not found (HTML or graph missing).")
        sys.exit(1)
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ttl", delete=False) as tf:
        graph_path = Path(tf.name)
    g.serialize(destination=str(graph_path), format="turtle")

    out_path = _ann_path(project_root, slug)
    out_path.parent.mkdir(parents=True, exist_ok=True)
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

    paths = list_deltas_for_scope(project_root, doc_scope(slug))
    if not paths:
        from src.deltas import scope_dir
        sd = scope_dir(project_root, doc_scope(slug))
        console.print(f"[yellow]No delta files for[/yellow] [bold]{slug}[/bold].")
        console.print(f"  (Looked under {sd.relative_to(project_root)}/delta.NNN.trig)")
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
    scope = doc_scope(slug)

    state_a = materialize(project_root, scope, at_seq=seq_a)
    state_b = materialize(project_root, scope, at_seq=seq_b)
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
              help="Output .ttl path (default: docs/<slug>/graph.ttl for HEAD, "
                   "docs/<slug>/graph.NNN.ttl for --at).")
@click.option("--no-diagram", is_flag=True,
              help="Skip diagram rendering — just write the graph snapshot.")
def snapshot(target: str, at_seq: int | None, out_path: Path | None, no_diagram: bool):
    """Write a materialized snapshot (Turtle + diagram) of a doc's scope.

    HEAD `graph.ttl` is auto-maintained on every `write_delta`, so the
    no-arg form is mostly there to refresh the diagram alongside it. With
    `--at <seq>`, writes the historical state after the step with that
    seq to `docs/<slug>/graph.NNN.ttl` and `docs/<slug>/diagram.NNN.*`.
    """
    from src.deltas import doc_scope, materialize
    from src.project import graph_ttl_path

    project_root = _find_project(Path.cwd())
    slug = _resolve_slug(project_root, target)
    scope = doc_scope(slug)

    g = materialize(project_root, scope, at_seq=at_seq)
    if len(g) == 0:
        console.print(f"[yellow]No triples to write[/yellow] for {slug}"
                      f"{f' at seq={at_seq}' if at_seq is not None else ''}.")
        return

    if out_path is None:
        out_path = graph_ttl_path(project_root, slug, at_seq=at_seq)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(out_path), format="turtle")
    console.print(f"  wrote   [dim]{out_path.relative_to(project_root)}[/dim] "
                  f"({len(g)} triples)")

    if no_diagram:
        return
    from src.diagram import DiagramError, make_diagram
    try:
        make_diagram(project_root, slug, console, at_seq=at_seq)
    except DiagramError as exc:
        console.print(f"  [yellow]diagram skipped[/yellow]: {exc}")
    except Exception as exc:
        console.print(f"  [yellow]diagram failed[/yellow]: {exc}")


@cli.command()
@click.option("--threshold", "threshold", type=int, default=2,
              help="Minimum number of docs that must declare a class for it to "
                   "consolidate to project scope (default 2).")
@click.option("--dry-run", is_flag=True,
              help="Show what would be consolidated without writing any deltas.")
def consolidate(threshold: int, dry_run: bool):
    """Consolidate equivalent ext: classes from per-doc graphs into the
    project scope (the cross-doc lift of `add`'s local proposals).

    Scans every doc-scope graph for ext-class declarations. Classes
    declared in ≥threshold docs are merged into a canonical definition
    at the project ext: namespace. Each contributing doc's scope gets a
    `consolidate` delta that REMOVES the doc-local class declaration AND
    rewrites instance triples to type as the new project URI.

    Pure slug-collision aggregation today. The next iteration also
    absorbs the embedding + LLM relation classifier for different-slug
    semantic equivalents (see docs/architecture/rdl-scopes.md).
    """
    from src.extract_part14.consolidate import walk_consolidate

    project_root = _find_project(Path.cwd())

    if dry_run:
        from src.deltas import list_scopes, materialize, project_scope
        from src.extract_part14.ext_ontology import extract_classes_from_graph
        from collections import defaultdict
        contributors_by_slug: dict[str, list[str]] = defaultdict(list)
        project_state = materialize(project_root, project_scope())
        already_promoted = set(extract_classes_from_graph(project_state).keys())
        for scope in list_scopes(project_root):
            if scope.kind != "doc" or not scope.name:
                continue
            per_doc_classes = extract_classes_from_graph(materialize(project_root, scope))
            for slug in per_doc_classes:
                if slug in already_promoted:
                    continue
                contributors_by_slug[slug].append(scope.name)
        candidates = [(slug, contribs) for slug, contribs in contributors_by_slug.items()
                       if len(contribs) >= threshold]
        if not candidates:
            console.print(f"[yellow]No ext class meets threshold ≥{threshold} docs.[/yellow]")
            return
        console.print(f"[bold]Would consolidate {len(candidates)} class(es)[/bold] "
                      f"(threshold ≥{threshold} docs):\n")
        for slug, contribs in sorted(candidates):
            console.print(f"  ext:[bold]{slug}[/bold]   "
                          f"{len(contribs)} contributors: {', '.join(contribs)}")
        console.print()
        return

    console.print(f"[bold]consolidate[/bold]   (threshold ≥{threshold} docs)")
    decisions = walk_consolidate(project_root, threshold=threshold, console=console)
    if not decisions:
        return
    console.print(f"  → consolidated {len(decisions)} class(es) into project scope")


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
