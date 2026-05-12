"""Bitmap-based class selection tests with a mock LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from rdflib import URIRef

from src.extract_part14.bitmap import (
    BitmapResult,
    collect_extractable_classes,
    expand_with_range_coupling,
    format_hierarchy,
    select_relevant_classes,
)
from src.extract_part14.loader import build_dataset, union_view
from src.llm import TextBlock
from src.models import ModelConfig
from src.project import init_project, PIPELINE_PART14


LIS = "http://rds.posccaesar.org/ontology/lis14/rdl/"


# ── Mock infrastructure ────────────────────────────────────────────────────

@dataclass
class _MockResp:
    content: list


class MockBitmapLLM:
    """Returns a canned bitmap selection."""
    def __init__(self, selected_curies: list[str], rationale: str = "test"):
        self.selected_curies = selected_curies
        self.rationale       = rationale
        self.calls: list[str] = []

    def create(self, *, model_id, messages, system="", tools=(), max_tokens=4096):
        self.calls.append(messages[0]["content"] if messages else "")
        payload = {
            "selected":  self.selected_curies,
            "rationale": self.rationale,
        }
        return _MockResp(content=[TextBlock(text=json.dumps(payload))])


@pytest.fixture(scope="module")
def ontology(tmp_path_factory):
    project_dir = tmp_path_factory.mktemp("bitmap-ontology")
    from rich.console import Console
    init_project(project_dir, Console(quiet=True), pipeline=PIPELINE_PART14)
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


# ── collect_extractable_classes ────────────────────────────────────────────

def test_collect_extractable_includes_all_levels(ontology):
    """Bitmap considers every extractable class — leaves AND intermediates."""
    classes = collect_extractable_classes(ontology, namespace=LIS)
    # Top-level
    assert URIRef(LIS + "Activity")            in classes
    assert URIRef(LIS + "Object")              in classes
    # Mid-level
    assert URIRef(LIS + "PhysicalObject")      in classes
    assert URIRef(LIS + "Organism")            in classes
    # Leaf
    assert URIRef(LIS + "Person")              in classes
    assert URIRef(LIS + "Compound")            in classes


def test_collect_extractable_skips_dg_plumbing(ontology):
    """dg:Document/Quote/etc. are dg:extractable false → excluded."""
    classes = collect_extractable_classes(ontology, namespace=LIS)
    DG = "http://example.org/docgraph/meta#"
    for plumbing in ("Document", "Chapter", "Quote", "File"):
        assert URIRef(DG + plumbing) not in classes


# ── format_hierarchy ──────────────────────────────────────────────────────

def test_format_hierarchy_shows_tree_structure(ontology):
    classes = collect_extractable_classes(ontology, namespace=LIS)
    text = format_hierarchy(ontology, classes)

    # Top-level (no indent)
    assert "- lis:Activity:" in text
    assert "- lis:Object:" in text

    # Indented children
    assert "  - lis:PhysicalObject:" in text
    assert "    - lis:Organism:" in text
    assert "      - lis:Person:" in text


# ── select_relevant_classes ────────────────────────────────────────────────

def test_bitmap_selector_returns_only_marked_classes(ontology, model):
    """LLM marks Activity, Person, Organization → those are returned."""
    mock = MockBitmapLLM(selected_curies=["lis:Activity", "lis:Person", "lis:Organization"])
    result = select_relevant_classes(
        ontology         = ontology,
        document_title   = "Invoice",
        document_excerpt = "Invoice for dental services",
        client           = mock,
        model            = model,
        namespace        = LIS,
    )

    assert isinstance(result, BitmapResult)
    assert URIRef(LIS + "Activity")     in result.selected
    assert URIRef(LIS + "Person")       in result.selected
    assert URIRef(LIS + "Organization") in result.selected
    # Things not in the LLM's selected list should not be returned
    assert URIRef(LIS + "Compound") not in result.selected
    assert URIRef(LIS + "Stream")   not in result.selected


def test_bitmap_selector_handles_empty_selection(ontology, model):
    """LLM may legitimately return no classes (document outside ontology coverage)."""
    mock = MockBitmapLLM(selected_curies=[], rationale="no extractable instances")
    result = select_relevant_classes(
        ontology=ontology,
        document_title="Random",
        document_excerpt="abstract poetry",
        client=mock, model=model,
        namespace=LIS,
    )
    assert result.selected == []
    assert result.rationale == "no extractable instances"


def test_bitmap_falls_back_to_all_on_parse_error(ontology, model):
    """If the LLM returns garbage, the bitmap falls back to all classes
    (degrades gracefully — walker still runs)."""

    class _BrokenLLM:
        def create(self, **_):
            return _MockResp(content=[TextBlock(text="not json at all, sorry")])

    result = select_relevant_classes(
        ontology=ontology,
        document_title="x", document_excerpt="y",
        client=_BrokenLLM(), model=model,
        namespace=LIS,
    )
    # Falls back to all extractable classes
    assert len(result.selected) > 0
    assert result.rationale == "(parse error)"


def test_bitmap_unknown_curie_in_response_is_ignored(ontology, model):
    """LLM hallucinates a CURIE not in the candidates — silently ignored."""
    mock = MockBitmapLLM(selected_curies=["lis:Activity", "lis:DoesNotExist"])
    result = select_relevant_classes(
        ontology=ontology,
        document_title="x", document_excerpt="y",
        client=mock, model=model,
        namespace=LIS,
    )
    assert URIRef(LIS + "Activity") in result.selected
    # Only Activity was LLM-picked — the rest of `selected` comes from range
    # coupling. The bogus CURIE shouldn't appear in either bucket.
    llm_picked = [c for c in result.selected if c not in result.coupling_added]
    assert llm_picked == [URIRef(LIS + "Activity")]
    assert URIRef(LIS + "DoesNotExist") not in result.selected


# ── Evidence-committal shape (new) ─────────────────────────────────────────

class MockEvidenceBitmapLLM:
    """Returns the new evidence-committal shape: list of {class, evidence}."""
    def __init__(self, items: list[tuple[str, str]], rationale: str = "test"):
        self.items     = items     # list of (curie, evidence)
        self.rationale = rationale
        self.calls: list[str] = []

    def create(self, *, model_id, messages, **_):
        self.calls.append(messages[0]["content"] if messages else "")
        payload = {
            "selected": [{"class": c, "evidence": ev} for c, ev in self.items],
            "rationale": self.rationale,
        }
        return _MockResp(content=[TextBlock(text=json.dumps(payload))])


def test_bitmap_captures_evidence(ontology, model):
    mock = MockEvidenceBitmapLLM(items=[
        ("lis:Person",       "Dmitrii Shishkin"),
        ("lis:Organization", "Zahnarztpraxis Liebermann"),
    ])
    result = select_relevant_classes(
        ontology=ontology,
        document_title="Invoice", document_excerpt="...",
        client=mock, model=model,
        namespace=LIS,
    )

    assert URIRef(LIS + "Person")       in result.selected
    assert URIRef(LIS + "Organization") in result.selected
    assert result.evidence[URIRef(LIS + "Person")]       == "Dmitrii Shishkin"
    assert result.evidence[URIRef(LIS + "Organization")] == "Zahnarztpraxis Liebermann"


def test_bitmap_legacy_string_format_still_works(ontology, model):
    """Backwards-compat: pre-evidence string CURIE format still parses."""
    mock = MockBitmapLLM(selected_curies=["lis:Activity"])
    result = select_relevant_classes(
        ontology=ontology,
        document_title="x", document_excerpt="y",
        client=mock, model=model,
        namespace=LIS,
    )
    assert URIRef(LIS + "Activity") in result.selected
    # No evidence captured (legacy format didn't carry it)
    assert URIRef(LIS + "Activity") not in result.evidence


# ── Range coupling ─────────────────────────────────────────────────────────

def test_range_coupling_pulls_in_unit_of_measure(ontology):
    """Selecting ScalarQuantityDatum auto-pulls UnitOfMeasure (range of datumUOM).

    The motivating case: bitmap LLM picks ScalarQuantityDatum but misses
    UnitOfMeasure → coupling adds it so `lis:datumUOM` links can be filled.
    """
    sqd = URIRef(LIS + "ScalarQuantityDatum")
    uom = URIRef(LIS + "UnitOfMeasure")

    final, added = expand_with_range_coupling(ontology, [sqd])

    assert sqd in final
    assert uom in final
    assert uom in added


def test_range_coupling_returns_input_unchanged_when_no_class_ranged_props(ontology):
    """A class with only literal-ranged or domain-less properties contributes
    nothing to coupling."""
    person = URIRef(LIS + "Person")
    final, added = expand_with_range_coupling(ontology, [person])

    # Person stays, no extras pulled in (Person has no domain-matched
    # class-ranged properties in LIS-14).
    assert person in final
    assert person not in added


def test_range_coupling_does_not_duplicate_already_selected(ontology):
    """If both ends are already selected, nothing is added."""
    sqd = URIRef(LIS + "ScalarQuantityDatum")
    uom = URIRef(LIS + "UnitOfMeasure")

    final, added = expand_with_range_coupling(ontology, [sqd, uom])

    assert sqd in final
    assert uom in final
    assert added == []   # nothing new
    assert final.count(uom) == 1


def test_range_coupling_wired_into_select_relevant_classes(ontology, model):
    """End-to-end: LLM picks ScalarQuantityDatum only, result includes UoM."""
    mock = MockBitmapLLM(selected_curies=["lis:ScalarQuantityDatum"])
    result = select_relevant_classes(
        ontology=ontology,
        document_title="Invoice", document_excerpt="115.84 EUR",
        client=mock, model=model,
        namespace=LIS,
    )

    sqd = URIRef(LIS + "ScalarQuantityDatum")
    uom = URIRef(LIS + "UnitOfMeasure")
    assert sqd in result.selected
    assert uom in result.selected
    assert uom in result.coupling_added
    assert sqd not in result.coupling_added
