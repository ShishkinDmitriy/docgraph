"""Tests for the delta-history CLI commands: history, diff, snapshot.

Build a tmp project with a few delta files; invoke each command via
click's CliRunner; assert the output contains the expected fields.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS
from rich.console import Console

from main import cli
from src.deltas import StepDelta, delta_path, doc_scope, write_delta
from src.ingest import _register_source, SOURCE_NS
from src.project import (
    PIPELINE_PART14,
    graphs_dir,
    init_project,
    sources_path,
)


EX = Namespace("http://example.org/src/cli-test/")


def _setup_project_with_doc(tmp_path: Path, slug: str = "demo") -> Path:
    """Init a part14 project under tmp_path; register one fake source so
    _resolve_slug succeeds, then write two doc-scope delta files."""
    project = tmp_path / "proj"
    project.mkdir()
    init_project(project, Console(quiet=True), pipeline=PIPELINE_PART14)

    # Register a fake source so the slug resolves through sources.ttl.
    fake_file = project / "fake.pdf"
    fake_file.write_bytes(b"")
    # graph_file is a registration anchor — points anywhere stable.
    from src.deltas import scope_dir
    sd = scope_dir(project, doc_scope(slug))
    sd.mkdir(parents=True, exist_ok=True)
    placeholder = sd / "anchor.txt"      # not matched by delta.NNN.trig glob
    placeholder.write_text("", encoding="utf-8")
    _register_source(
        project, slug, fake_file, placeholder,
        file_hash="sha256:abc", file_size=0, mime_type="application/pdf",
    )

    # Seq 1: convert — adds 2 triples.
    g1 = Graph()
    g1.bind("ex", EX, override=True)
    g1.add((EX["d1"], RDF.type, EX.File))
    g1.add((EX["d1"], RDFS.label, Literal("Demo File")))
    write_delta(
        StepDelta(scope=doc_scope(slug), step="convert", seq=1, added=g1),
        delta_path(project, doc_scope(slug), 1),
    )

    # Seq 2: extract — adds 1 triple, removes 1 (re-label).
    g2_add = Graph(); g2_add.bind("ex", EX, override=True)
    g2_add.add((EX["d1"], RDFS.label, Literal("Demo File (renamed)")))
    g2_rm = Graph(); g2_rm.bind("ex", EX, override=True)
    g2_rm.add((EX["d1"], RDFS.label, Literal("Demo File")))
    write_delta(
        StepDelta(scope=doc_scope(slug), step="extract", seq=2,
                  added=g2_add, removed=g2_rm, parent_seq=1),
        delta_path(project, doc_scope(slug), 2),
    )

    return project


# ── history ─────────────────────────────────────────────────────────────


def test_history_lists_deltas_with_seqs(tmp_path, monkeypatch):
    project = _setup_project_with_doc(tmp_path)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["history", "demo"], catch_exceptions=False)
    assert result.exit_code == 0
    out = result.stdout
    assert "seq   1" in out and "convert" in out
    assert "seq   2" in out and "extract" in out
    assert "+2"  in out                  # seq 1 added 2 triples
    assert "+1"  in out and "-1" in out  # seq 2 added 1, removed 1


def test_history_when_no_deltas_yet(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    init_project(project, Console(quiet=True), pipeline=PIPELINE_PART14)
    # Register a slug without any delta files
    fake_file = project / "x.pdf"; fake_file.write_bytes(b"")
    from src.deltas import scope_dir
    sd = scope_dir(project, doc_scope("empty"))
    sd.mkdir(parents=True, exist_ok=True)
    placeholder = sd / "placeholder"
    placeholder.write_text("", encoding="utf-8")
    _register_source(project, "empty", fake_file, placeholder,
                     file_hash="sha256:0", file_size=0, mime_type="application/pdf")

    monkeypatch.chdir(project)
    result = CliRunner().invoke(cli, ["history", "empty"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "No delta files" in result.stdout


# ── diff ────────────────────────────────────────────────────────────────


def test_diff_between_two_seqs(tmp_path, monkeypatch):
    project = _setup_project_with_doc(tmp_path)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        cli, ["diff", "demo", "1", "2"], catch_exceptions=False)
    assert result.exit_code == 0
    # seq 1 → seq 2: replaced "Demo File" with "Demo File (renamed)"
    assert "+1 triples" in result.stdout
    assert "-1 triples" in result.stdout
    assert "Demo File (renamed)" in result.stdout
    assert "Demo File"           in result.stdout


def test_diff_zero_to_head_returns_full_state(tmp_path, monkeypatch):
    project = _setup_project_with_doc(tmp_path)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        cli, ["diff", "demo", "0", "2"], catch_exceptions=False)
    assert result.exit_code == 0
    # State at seq 2: 2 triples (type + renamed label). Diff from seq 0 = all of them added.
    assert "+2 triples" in result.stdout
    assert "-0" in result.stdout or "Removed: [red]-0" in result.stdout or "0 triples" in result.stdout


# ── snapshot ────────────────────────────────────────────────────────────


def test_snapshot_writes_head_state(tmp_path, monkeypatch):
    project = _setup_project_with_doc(tmp_path)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        cli, ["snapshot", "demo"], catch_exceptions=False)
    assert result.exit_code == 0
    from src.deltas import scope_dir
    out_file = scope_dir(project, doc_scope("demo")) / "snapshot.HEAD.ttl"
    assert out_file.is_file()
    g = Graph()
    g.parse(out_file, format="turtle")
    # HEAD state: 2 triples (type + renamed label)
    assert len(g) == 2
    assert (EX["d1"], RDFS.label, Literal("Demo File (renamed)")) in g


def test_snapshot_at_historical_seq(tmp_path, monkeypatch):
    project = _setup_project_with_doc(tmp_path)
    monkeypatch.chdir(project)
    result = CliRunner().invoke(
        cli, ["snapshot", "demo", "--at", "1"], catch_exceptions=False)
    assert result.exit_code == 0
    from src.deltas import scope_dir
    out_file = scope_dir(project, doc_scope("demo")) / "snapshot.001.ttl"
    assert out_file.is_file()
    g = Graph()
    g.parse(out_file, format="turtle")
    # Seq 1 state: 2 triples (type + ORIGINAL label, before extract renamed)
    assert (EX["d1"], RDFS.label, Literal("Demo File")) in g
    assert (EX["d1"], RDFS.label, Literal("Demo File (renamed)")) not in g
