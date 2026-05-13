"""Template registry — load every template TTL under a directory tree and
index them by URI and by `tpl:subject`.

Used by the LLM-emit pipeline to look up templates whose subject matches the
relationship-class a given prompt is targeting:

    reg = Registry.load_default()
    for tpl in reg.by_subject(ISO15926.Classification):
        ...

`load_default()` reads `data/templates/**/*.ttl` from the repo root. Tests
and CLI entry-points can pass an explicit directory via `Registry.load_dir`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from src.templates.loader import TPL, Template, load_template

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TEMPLATES_DIR = REPO_ROOT / "data" / "templates"


_DEFAULT_REGISTRY: "Registry | None" = None


def default_registry() -> "Registry":
    """Load and cache the default template registry on first call.

    Multiple parts of the pipeline (root walker, property walker) need
    template lookups by subject class. Re-loading the registry per call
    would be wasteful (file I/O + parsing for every entity); cached at
    module level since the registry is read-only after load.
    """
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = Registry.load_default()
    return _DEFAULT_REGISTRY


@dataclass
class Registry:
    """In-memory template index. One pass at startup; reads all TTLs under
    a root directory."""

    by_uri: dict[URIRef, Template] = field(default_factory=dict)
    _by_subject: dict[URIRef, list[Template]] = field(default_factory=dict)

    def all(self) -> list[Template]:
        return list(self.by_uri.values())

    def by_subject(self, subject: URIRef) -> list[Template]:
        return list(self._by_subject.get(subject, ()))

    def subjects(self) -> list[URIRef]:
        return list(self._by_subject.keys())

    @classmethod
    def load_dir(cls, root: str | Path) -> "Registry":
        """Walk *root* recursively and load every TTL that declares at
        least one `tpl:Template`. Files without one (e.g. shared meta
        vocabularies like `_meta.ttl`) are silently skipped."""
        root = Path(root)
        reg = cls()
        for path in sorted(root.rglob("*.ttl")):
            if not _has_template(path):
                continue
            tpl = load_template(path)
            reg._add(tpl)
        return reg

    @classmethod
    def load_default(cls) -> "Registry":
        return cls.load_dir(DEFAULT_TEMPLATES_DIR)

    def _add(self, tpl: Template) -> None:
        if tpl.uri in self.by_uri:
            existing = self.by_uri[tpl.uri]
            raise ValueError(
                f"duplicate template URI {tpl.uri} "
                f"(slug {existing.slug!r} vs {tpl.slug!r})"
            )
        self.by_uri[tpl.uri] = tpl
        if tpl.subject is not None:
            self._by_subject.setdefault(tpl.subject, []).append(tpl)


def _has_template(path: Path) -> bool:
    """Quick scan: parse default graph only and check for a `tpl:Template`
    typing triple. Avoids loading the lifted/lowered named graphs of files
    that don't declare a template at all."""
    g = Graph()
    try:
        g.parse(str(path), format="trig")
    except Exception:
        return False
    return any(g.triples((None, RDF.type, TPL.Template)))
