"""tasks — print the registered task DAG as a tree.

Meta-task: takes no args, doesn't touch the project. Useful for
discovering what's available (`dg tasks`) and for double-checking
how tasks chain into each other.

Roots (tasks no other task depends on) are printed as separate trees;
each child shows its own deps recursively. Shared subtrees show up
under every root that pulls them in — easier to read than collapsing
into "see above" references.
"""

from __future__ import annotations

from rich.padding import Padding
from rich.tree import Tree

from src.tasks._registry import docgraph


@docgraph.task(desc="Print the registered task DAG as a tree", quiet=True)
def tasks(ctx) -> None:
    reg = docgraph
    incoming: dict[str, set[str]] = {n: set() for n in reg.tasks}
    for n, t in reg.tasks.items():
        for d in t.deps:
            incoming[d].add(n)
    roots = sorted(n for n, parents in incoming.items() if not parents)

    console = ctx["console"]
    for root in roots:
        tree = Tree(_label(reg.tasks[root]), guide_style="dim")
        _add_deps(tree, root, reg)
        console.print(Padding(tree, (0, 0, 0, 2)))


def _label(task) -> str:
    return f"{task.name}  [dim]— {task.desc}[/dim]" if task.desc else task.name


def _add_deps(node: Tree, task_name: str, reg) -> None:
    for dep in reg.tasks[task_name].deps:
        child = node.add(_label(reg.tasks[dep]))
        _add_deps(child, dep, reg)
