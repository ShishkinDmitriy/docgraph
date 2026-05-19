"""Tests for the three-pass root walker (Pass A of the part14 pipeline).

Verifies the new entity-extraction model:
- Three LLM calls, one per disjoint LIS-14 root (Object/Aspect/Activity)
- Multi-typing on rdf:type (Part 14 §E.8 sanctions stacking permanent types)
- Role pattern: lis:Role + lis:realizedIn + lis:hasRole minted from
  Activity branch's participant role_hint
- Participant resolution by case-insensitive label match against entities
  already extracted under another root
- dg:typeHint triples for LLM-suggested specific class names
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from rdflib import Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from src.extract_part14.loader import build_dataset, union_view
from src.extract_part14.root_walker import (
    Role,
    _subtree_text,
    walk_roots,
)
from src.extract_part14.walker import DG, LIS
from src.llm import TextBlock
from src.models import ModelConfig
from src.tasks.init import init_project


# ── Mock infrastructure ────────────────────────────────────────────────────

@dataclass
class _MockResp:
    content: list


class MockRootLLM:
    """Returns canned responses keyed by which root class is being extracted.

    The walker prompts mention `"{root_label}"` exactly once near the top —
    matching against `'instances of "X"'` would miss because the new prompt
    says `every entity of root class "X"`. We match on `root class "X"`.
    """
    def __init__(self, responses_by_root: dict[str, list[dict]]):
        self.responses_by_root = responses_by_root
        self.calls_in_order:   list[str] = []
        self.captured_prompts: list[str] = []

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        prompt = messages[0]["content"] if messages else ""
        self.captured_prompts.append(prompt)

        # Match against the root label this call is for.
        root_label = None
        for label in self.responses_by_root:
            needle = f'root class "{label}"'
            if needle in prompt or f'every ACTIVITY' in prompt and label == "Activity":
                root_label = label
                break
        self.calls_in_order.append(root_label or "(unknown)")

        instances = self.responses_by_root.get(root_label, []) if root_label else []
        payload = {"instances": instances}
        return _MockResp(content=[TextBlock(text=json.dumps(payload))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("root-walker-ontology")
    from rich.console import Console
    init_project(project_dir, Console(quiet=True))
    ds = build_dataset(project_dir)
    return union_view(ds)


@pytest.fixture
def model():
    return ModelConfig(
        uri=URIRef("http://example.org/model/test"),
        model_id="test-model",
        label="test",
        provider="test",
    )


# ── Three-root call sequencing ─────────────────────────────────────────────

def test_walk_roots_calls_three_roots_in_order(ontology, model):
    """Walker makes exactly three LLM calls (Object → Aspect → Activity)."""
    mock = MockRootLLM(responses_by_root={
        "Object": [], "Aspect": [], "Activity": [],
    })
    base_ns = Namespace("http://example.org/src/order/")
    md_uri  = URIRef("http://example.org/src/order/md")

    g, extracted, roles = walk_roots(
        full_markdown="some markdown",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    assert mock.calls_in_order == ["Object", "Aspect", "Activity"]
    assert extracted == []
    assert roles == []


# ── Multi-typing ───────────────────────────────────────────────────────────

def test_walk_roots_emits_multiple_rdf_types(ontology, model):
    """LLM returns multiple type CURIEs → all become rdf:type triples."""
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "Invoice 1352",
             "types": ["lis:PhysicalObject", "lis:InformationObject"],
             "evidence": [{"exact": "Rechnung Nr 1352"}]}
        ],
        "Aspect": [], "Activity": [],
    })
    base_ns = Namespace("http://example.org/src/multi/")
    md_uri  = URIRef("http://example.org/src/multi/md")

    g, extracted, _ = walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    assert len(extracted) == 1
    e = extracted[0]
    assert LIS.PhysicalObject in e.types
    assert LIS.InformationObject in e.types
    # Both rdf:type triples land in the graph
    assert (e.uri, RDF.type, LIS.PhysicalObject) in g
    assert (e.uri, RDF.type, LIS.InformationObject) in g


def test_walk_roots_falls_back_to_root_when_no_valid_types(ontology, model):
    """LLM returns only unknown CURIEs → entity gets the root class as type."""
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "mystery thing",
             "types": ["lis:DoesNotExist", "fake:Foo"],
             "evidence": [{"exact": "thing"}]}
        ],
        "Aspect": [], "Activity": [],
    })
    base_ns = Namespace("http://example.org/src/fallback/")
    md_uri  = URIRef("http://example.org/src/fallback/md")

    g, extracted, _ = walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    assert len(extracted) == 1
    assert extracted[0].types == [LIS.Object]   # fell back to root
    assert (extracted[0].uri, RDF.type, LIS.Object) in g


# ── Role pattern ───────────────────────────────────────────────────────────

def test_walk_roots_mints_role_for_activity_participant(ontology, model):
    """Activity's participant with role_hint → lis:Role + lis:realizedIn +
    lis:hasRole. Type hints become dg:typeHint literals on the role."""
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "Polina Liebermann",
             "types": ["lis:Person"],
             "evidence": [{"exact": "Polina Liebermann"}]}
        ],
        "Aspect": [],
        "Activity": [
            {"name": "tooth cleaning on 17.01.2025",
             "types": ["lis:Activity"],
             "evidence": [{"exact": "professional tooth cleaning"}],
             "participants": [
                 {"name": "Polina Liebermann",
                  "role_hint": "practitioner",
                  "type_hints": ["Dentist", "HealthcareProvider"]}
             ]}
        ],
    })
    base_ns = Namespace("http://example.org/src/role/")
    md_uri  = URIRef("http://example.org/src/role/md")

    g, extracted, roles = walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    # Two entities: Polina (Person) + the Activity
    polina   = next(e for e in extracted if e.label == "Polina Liebermann")
    activity = next(e for e in extracted if e.label == "tooth cleaning on 17.01.2025")

    # Exactly one role minted, for the practitioner participant
    assert len(roles) == 1
    role = roles[0]
    assert isinstance(role, Role)
    assert role.label    == "practitioner"
    assert role.player   == polina.uri
    assert role.activity == activity.uri
    assert "Dentist" in role.type_hints

    # Verify the four canonical triples of the BFO-style role pattern
    assert (role.uri,    RDF.type,        LIS.Role)      in g
    assert (role.uri,    LIS.realizedIn,  activity.uri)  in g
    assert (role.uri,    RDFS.label,      None) in [(s, p, None) for s, p, o in g.triples((role.uri, RDFS.label, None))]
    assert (polina.uri,  LIS.hasRole,     role.uri)      in g
    assert (activity.uri, LIS.hasParticipant, polina.uri) in g

    # Type hints written as dg:typeHint literals on the role
    hint_values = {str(o) for o in g.objects(role.uri, DG.typeHint)}
    assert "Dentist"             in hint_values
    assert "HealthcareProvider"  in hint_values


def test_walk_roots_skips_role_when_no_hint(ontology, model):
    """Participant without role_hint → hasParticipant link only, no role
    individual. The mere fact of participation doesn't warrant a role."""
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "Polina", "types": ["lis:Person"],
             "evidence": [{"exact": "Polina"}]}
        ],
        "Aspect": [],
        "Activity": [
            {"name": "some activity", "types": ["lis:Activity"],
             "evidence": [{"exact": "happened"}],
             "participants": [{"name": "Polina"}]}      # no role_hint
        ],
    })
    base_ns = Namespace("http://example.org/src/no-role/")
    md_uri  = URIRef("http://example.org/src/no-role/md")

    g, extracted, roles = walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    assert roles == []
    polina   = next(e for e in extracted if e.label == "Polina")
    activity = next(e for e in extracted if e.label == "some activity")
    assert (activity.uri, LIS.hasParticipant, polina.uri) in g
    # No role triples for this participant
    assert not list(g.triples((polina.uri, LIS.hasRole, None)))


def test_walk_roots_participant_name_match_is_case_insensitive(ontology, model):
    """LLM may cite a participant by a slightly different casing than the
    canonical name — case-insensitive label match resolves correctly."""
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "Dmitrii Shishkin", "types": ["lis:Person"],
             "evidence": [{"exact": "Herrn Dmitrii Shishkin"}]}
        ],
        "Aspect": [],
        "Activity": [
            {"name": "billing", "types": ["lis:Activity"],
             "evidence": [{"exact": "invoice issued"}],
             "participants": [{"name": "dmitrii shishkin", "role_hint": "payer"}]}
        ],
    })
    base_ns = Namespace("http://example.org/src/case/")
    md_uri  = URIRef("http://example.org/src/case/md")

    g, extracted, roles = walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    dmitrii  = next(e for e in extracted if e.label == "Dmitrii Shishkin")
    activity = next(e for e in extracted if e.label == "billing")

    assert (activity.uri, LIS.hasParticipant, dmitrii.uri) in g
    assert len(roles) == 1
    assert roles[0].player == dmitrii.uri


# ── Type hints on entities themselves ──────────────────────────────────────

def test_walk_roots_writes_type_hints_as_dg_triples(ontology, model):
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "EUR", "types": ["lis:UnitOfMeasure"],
             "evidence": [{"exact": "EUR"}],
             "type_hints": ["Currency", "MonetaryUnit"]}
        ],
        "Aspect": [], "Activity": [],
    })
    base_ns = Namespace("http://example.org/src/hints/")
    md_uri  = URIRef("http://example.org/src/hints/md")

    g, extracted, _ = walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    eur = extracted[0]
    hints = {str(o) for o in g.objects(eur.uri, DG.typeHint)}
    assert hints == {"Currency", "MonetaryUnit"}
    # The dataclass also carries them for in-memory downstream use
    # (set via the walker's iteration over evidence selectors)


# ── Exclusion across roots ─────────────────────────────────────────────────

def test_walk_roots_passes_existing_entities_to_subsequent_prompts(ontology, model):
    """When the Activity prompt is built, Object entities from pass 1 appear
    in the 'Already extracted' block so the LLM doesn't re-emit them."""
    mock = MockRootLLM(responses_by_root={
        "Object": [
            {"name": "Polina", "types": ["lis:Person"],
             "evidence": [{"exact": "Polina"}]}
        ],
        "Aspect": [], "Activity": [],
    })
    base_ns = Namespace("http://example.org/src/exclude/")
    md_uri  = URIRef("http://example.org/src/exclude/md")

    walk_roots(
        full_markdown="md",
        base_ns=base_ns, md_source_uri=md_uri,
        ontology=ontology, client=mock, model=model,
    )

    # The Activity prompt (3rd call) should mention Polina in the
    # "Already extracted" block.
    activity_prompt = mock.captured_prompts[2]
    assert "Already extracted" in activity_prompt
    assert "Polina" in activity_prompt


# ── Aspect subtree includes Role (the role pattern is enforced via a template) ──

def test_aspect_subtree_includes_role_class(ontology):
    """lis:Role is rendered like any other Aspect class. The role pattern
    (Role + realizedIn + hasRole) is enforced by the
    `lis14tpl:RoleRealizedInActivity` template the LLM is told to invoke,
    not by excluding Role from the subtree."""
    text = _subtree_text(LIS.Aspect, ontology)
    assert "lis:Role:" in text
    # Sibling RealizableEntity subclasses still appear.
    assert "lis:Disposition:" in text or "Disposition" in text


def test_object_subtree_populated(ontology):
    """Spot-check the Object subtree renders its concrete leaves."""
    text = _subtree_text(LIS.Object, ontology)
    assert "lis:Object:" in text
    assert "lis:Person:" in text


# ── Prompt overlays: skos:scopeNote + skos:example rendering ───────────────

def test_subtree_renders_scope_note_as_use_line(ontology):
    """QuantityDatum carries a skos:scopeNote in dg-part14-alignments.ttl —
    must appear in the subtree rendering as a 'USE:' line."""
    text = _subtree_text(LIS.Object, ontology)
    assert "lis:QuantityDatum:" in text
    # The USE: line follows the class line
    assert "USE:" in text
    # Behavioral content from the scope note
    assert "Identifiers" in text or "invoice number" in text.lower()


def test_subtree_renders_examples_as_example_lines(ontology):
    """skos:example annotations on QuantityDatum render as 'EXAMPLE:' lines."""
    text = _subtree_text(LIS.Object, ontology)
    assert "EXAMPLE:" in text
    # Both GOOD and BAD examples should appear (we wrote at least one of each)
    assert "GOOD:" in text
    assert "BAD:" in text


def test_subtree_no_overlay_lines_for_unannotated_class(ontology):
    """A class without skos:scopeNote / skos:example renders cleanly —
    no spurious USE: or EXAMPLE: lines."""
    # Use a class that should NOT have an overlay (e.g., lis:Person).
    text = _subtree_text(LIS.Object, ontology)
    # Find the Person entry — its block should not have USE:/EXAMPLE: lines
    # before the next class begins. Cheap check: count overlay lines, they
    # should match the number of overlays we wrote (currently 4 classes
    # under the Object tree: QuantityDatum, Function (Aspect — not here),
    # ...). Just verify Person doesn't introduce them on its line.
    lines = text.splitlines()
    person_idx = next(i for i, ln in enumerate(lines) if "lis:Person:" in ln)
    # The line immediately after Person should be either Person's children
    # (indented further) or a sibling at the same depth — not a USE: line.
    next_line = lines[person_idx + 1] if person_idx + 1 < len(lines) else ""
    assert "USE:" not in next_line
