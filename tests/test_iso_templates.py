"""File-driven tests for the 15926.blog template imports under data/templates/iso/.

Each iso/ template has a sibling fixture trio in tests/fixtures/templates/iso/:

  <stem>.lifted.ttl   — a worked lifted-form instance (input to `expand`,
                          serves as the readable lifted example)
  <stem>.lowered.ttl  — the expected lowered Part 2 expansion of that instance
  <stem>.sparql       — the expected `to_sparql` golden output

Adding a new template-test is purely additive: drop the three sibling files
and pytest's parametrization picks them up automatically.

Regenerate the .sparql golden after intentional translator changes with:

    .venv/bin/python -c "from pathlib import Path; \
      from src.templates.loader import load_template; \
      from src.templates.recognize import to_sparql; \
      F=Path('tests/fixtures/templates/iso'); \
      [(F/f'{p.stem}.sparql').write_text( \
          to_sparql(load_template(f'data/templates/iso/{p.stem}.ttl'))+'\n') \
       for p in F.glob('*.lifted.ttl')]"
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import Graph, URIRef
from rdflib.compare import isomorphic, to_isomorphic

from src.templates.expand import expand
from src.templates.loader import Template, load_template
from src.templates.recognize import to_sparql

REPO_ROOT = Path(__file__).resolve().parent.parent
ISO_TEMPLATES = REPO_ROOT / "data" / "templates" / "iso"
ISO_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "templates" / "iso"

STEMS = sorted(p.stem.removesuffix(".lifted") for p in ISO_FIXTURES.glob("*.lifted.ttl"))


def _read_graph(path: Path) -> Graph:
    g = Graph()
    g.parse(str(path), format="turtle")
    return g


def _extract_bindings(template: Template, instance: Graph) -> dict:
    """Pattern-match the template's lifted graph against `instance` and return
    {variable-local-name: rdf-term} bindings.

    Iterates triple-by-triple: each pass tries to match every lifted triple
    with currently-known bindings substituted in; new bindings are extracted
    when a triple matches uniquely. Stops when a pass adds nothing.
    """
    var_ns = str(template.var_ns)
    bindings: dict[str, object] = {}

    def resolve(term):
        if isinstance(term, URIRef) and str(term).startswith(var_ns):
            return bindings.get(str(term)[len(var_ns):])
        return term

    lifted_triples = list(template.lifted)
    for _ in range(len(lifted_triples) + 1):
        progress = False
        for s, p, o in lifted_triples:
            matches = list(instance.triples((resolve(s), resolve(p), resolve(o))))
            if len(matches) != 1:
                continue
            ms, mp, mo = matches[0]
            for tpl_term, inst_term in ((s, ms), (p, mp), (o, mo)):
                if isinstance(tpl_term, URIRef) and str(tpl_term).startswith(var_ns):
                    local = str(tpl_term)[len(var_ns):]
                    if local not in bindings:
                        bindings[local] = inst_term
                        progress = True
        if not progress:
            break
    return bindings


def _diff(actual: Graph, expected: Graph) -> str:
    only_actual = set(actual) - set(expected)
    only_expected = set(expected) - set(actual)
    lines = []
    if only_actual:
        lines.append("triples in actual but not expected:")
        lines += [f"  + {s} {p} {o}" for s, p, o in sorted(only_actual, key=str)]
    if only_expected:
        lines.append("triples in expected but not actual:")
        lines += [f"  - {s} {p} {o}" for s, p, o in sorted(only_expected, key=str)]
    return "\n".join(lines) or "(no triple-set difference; isomorphism failure)"


@pytest.mark.parametrize("stem", STEMS)
def test_iso_expand(stem: str) -> None:
    """Lifted example → expand against the template → equals lowered fixture."""
    template = load_template(ISO_TEMPLATES / f"{stem}.ttl")
    lifted_input = _read_graph(ISO_FIXTURES / f"{stem}.lifted.ttl")
    expected = _read_graph(ISO_FIXTURES / f"{stem}.lowered.ttl")

    bindings = _extract_bindings(template, lifted_input)
    actual = expand(template, bindings)

    assert isomorphic(to_isomorphic(actual), to_isomorphic(expected)), _diff(
        actual, expected
    )


@pytest.mark.parametrize("stem", STEMS)
def test_iso_to_sparql(stem: str) -> None:
    """`to_sparql(template)` matches the .sparql golden fixture."""
    template = load_template(ISO_TEMPLATES / f"{stem}.ttl")
    expected = (ISO_FIXTURES / f"{stem}.sparql").read_text(encoding="utf-8")
    actual = to_sparql(template) + "\n"
    assert actual == expected
