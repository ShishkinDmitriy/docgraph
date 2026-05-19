#!/usr/bin/env python3
"""docgraph — knowledge graph extraction from documents (ISO 15926 Part 14).

Usage: docgraph <task> [args...]

Each task interprets its own positional args (paths, slugs, seqs). Common shapes:

    docgraph init [DIR]              — initialise a project (default: cwd)
    docgraph add FILE                — ingest a PDF or TTL
    docgraph status / clean          — project-wide, no args
    docgraph diagram TARGET          — TARGET = slug or path
    docgraph snapshot TARGET [SEQ]   — SEQ defaults to HEAD
    docgraph diff TARGET SEQ_A SEQ_B
    docgraph help <task>             — task module docstring

`-f` forces the task (overrides its dirty check). `-d` enables verbose
logging (LLM prompts and responses).
"""

import logging
import sys

import click
from rich.console import Console

from src.sources import IngestError
from src.tasks import docgraph

console = Console()


def _enable_debug() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")
    logging.getLogger("src").setLevel(logging.DEBUG)
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


@click.command(context_settings={
    "ignore_unknown_options": True,
    "help_option_names":      ["-h", "--help"],
})
@click.argument("task_name", required=False)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.option("-f", "--force", is_flag=True,
              help="Force the task (override its dirty check).")
@click.option("-d", "--debug", is_flag=True,
              help="Verbose logging (LLM prompts and responses).")
def cli(task_name: str | None, args: tuple, force: bool, debug: bool):
    """docgraph <task> [args...]"""
    if task_name is None:
        click.echo(cli.get_help(click.get_current_context()))
        click.echo("\nTasks: " + ", ".join(sorted(docgraph.tasks)))
        sys.exit(0)

    if task_name == "help":
        target = args[0] if args else None
        if target and target in docgraph.tasks:
            click.echo((docgraph.tasks[target].fn.__doc__ or "(no docstring)").strip())
        else:
            click.echo("Usage: docgraph help <task>")
            click.echo("Tasks: " + ", ".join(sorted(docgraph.tasks)))
        return

    if task_name not in docgraph.tasks:
        console.print(f"[red]Error:[/red] unknown task {task_name!r}")
        console.print(f"Available: {', '.join(sorted(docgraph.tasks))}")
        sys.exit(1)

    if debug:
        _enable_debug()

    forced = {task_name} if force else set()
    try:
        docgraph.run(task_name, {
            "console":      console,
            "args":         args,
            "forced_tasks": forced,
        }, console=console, force=forced)
    except (IngestError, NotImplementedError,
            FileExistsError, NotADirectoryError, click.UsageError) as exc:
        console.print(f"[red]Error:[/red] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    cli()
