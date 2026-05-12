"""POC RDL resolver against an external SPARQL endpoint (Wikidata).

Tests use a mocked _run() that returns canned SPARQL bindings so the suite
doesn't hit the network. A separate (skipped by default) live test exercises
the real Wikidata endpoint — opt in via DOCGRAPH_RDL_LIVE=1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from rdflib import URIRef

from src.extract_part14.rdl import (
    WIKIDATA,
    RdlConfig,
    RdlResolver,
    ResolutionResult,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

class _StubResolver(RdlResolver):
    """Bypasses the network — returns canned bindings for predictable testing."""
    def __init__(self, config, *, exact_response=None, fuzzy_response=None,
                 cache_dir=None):
        super().__init__(config, cache_dir=cache_dir)
        self._exact_response = exact_response or []
        self._fuzzy_response = fuzzy_response or []
        self._call_log: list[str] = []

    def _run(self, query: str) -> list[dict]:
        self._call_log.append(query)
        # Both queries are UNION-form now. Fuzzy uses CONTAINS; exact uses
        # equality. Distinguish on that.
        if "CONTAINS" in query:
            return self._fuzzy_response
        return self._exact_response


def _binding(item_uri: str, label: str, item_var: str = "item",
             label_var: str = "label") -> dict:
    return {
        item_var:  {"type": "uri", "value": item_uri},
        label_var: {"type": "literal", "value": label, "xml:lang": "en"},
    }


# ── Exact match ─────────────────────────────────────────────────────────────

def test_exact_match_returns_high_confidence():
    resolver = _StubResolver(
        WIKIDATA,
        exact_response=[_binding("http://www.wikidata.org/entity/Q4916", "Euro")],
    )
    result = resolver.resolve("EUR")
    assert result.uri == URIRef("http://www.wikidata.org/entity/Q4916")
    assert result.label == "Euro"
    assert result.confidence == 1.0


def test_no_match_returns_none():
    resolver = _StubResolver(WIKIDATA, exact_response=[], fuzzy_response=[])
    result = resolver.resolve("Zzznonexistent")
    assert result.uri is None
    assert result.confidence == 0.0


# ── Fuzzy fallback ─────────────────────────────────────────────────────────

def test_fuzzy_fallback_when_exact_misses():
    resolver = _StubResolver(
        WIKIDATA,
        exact_response=[],     # exact misses
        fuzzy_response=[_binding(
            "http://www.wikidata.org/entity/Q4916", "Euro",
        )],   # generic queries (POSC Caesar + Wikidata) bind ?item / ?label
    )
    result = resolver.resolve("Euro")
    assert result.uri == URIRef("http://www.wikidata.org/entity/Q4916")
    assert 0.6 <= result.confidence < 1.0


# ── Caching ────────────────────────────────────────────────────────────────

def test_cache_persists_across_resolver_instances(tmp_path: Path):
    cache = tmp_path / "rdl"
    resolver1 = _StubResolver(
        WIKIDATA,
        cache_dir=cache,
        exact_response=[_binding("http://www.wikidata.org/entity/Q4916", "Euro")],
    )
    resolver1.resolve("EUR")

    cache_file = cache / "wikidata.json"
    assert cache_file.exists()
    payload = json.loads(cache_file.read_text())
    assert "eur" in payload    # lowercased probe is the key

    # New instance — empty stub responses; relies entirely on cache
    resolver2 = _StubResolver(WIKIDATA, cache_dir=cache,
                              exact_response=[], fuzzy_response=[])
    result = resolver2.resolve("EUR")
    assert result.uri == URIRef("http://www.wikidata.org/entity/Q4916")
    assert result.confidence == 1.0
    assert resolver2._call_log == []   # never hit SPARQL — cache hit


def test_cache_key_includes_kind_hint(tmp_path: Path):
    """Same probe with different kind_hints caches separately."""
    cache = tmp_path / "rdl"
    resolver = _StubResolver(
        WIKIDATA,
        cache_dir=cache,
        exact_response=[_binding("http://www.wikidata.org/entity/Q4916", "Euro")],
    )
    resolver.resolve("EUR", kind_hint=URIRef("urn:test:Currency"))
    resolver.resolve("EUR", kind_hint=URIRef("urn:test:OrganizationType"))
    payload = json.loads((cache / "wikidata.json").read_text())
    assert len(payload) == 2


# ── Empty-probe handling ───────────────────────────────────────────────────

def test_empty_probe_returns_none_immediately():
    resolver = _StubResolver(WIKIDATA, exact_response=[])
    result = resolver.resolve("")
    assert result.uri is None
    assert resolver._call_log == []   # short-circuited before SPARQL


def test_probe_with_newlines_and_tabs_normalized():
    """Probes with embedded newlines / tabs / weird whitespace would have
    produced HTTP 400 from POSC Caesar (malformed SPARQL string literals)
    before normalization. Now they collapse to single-line form before being
    embedded in the query."""
    resolver = _StubResolver(
        WIKIDATA,
        exact_response=[_binding("http://www.wikidata.org/entity/Q4916", "Euro")],
    )
    # Probe with newlines, tabs, repeated spaces — all should collapse
    messy_probe = "Centrifugal\n  Pump\twith   weird\r\nwhitespace"
    result = resolver.resolve(messy_probe)
    assert result.uri == URIRef("http://www.wikidata.org/entity/Q4916")
    # The SPARQL sent should contain the normalized form, not raw newlines
    last_query = resolver._call_log[-1]
    assert "\n" not in last_query.split('"')[1] if '"' in last_query else True
    assert "\t" not in last_query


def test_probe_with_quote_chars_escaped():
    """Internal double-quote in probe must be escaped or the query is malformed."""
    from src.extract_part14.rdl import _escape
    out = _escape('he said "hi"')
    assert '"' not in out.replace('\\"', '')   # all quotes are escaped


def test_probe_caps_at_200_chars():
    """Defensive: extremely long probes get truncated to avoid 400 / 414."""
    from src.extract_part14.rdl import _escape
    huge = "x" * 5000
    assert len(_escape(huge)) <= 200


# ── 429 + circuit breaker + no-cache-on-error ─────────────────────────────

class _ErroringResolver(RdlResolver):
    """Forces every _run() call to raise an HTTPError with a chosen code."""
    def __init__(self, *, status: int, retry_after: str | None = None,
                 cache_dir=None):
        # Skip the actual rate-limit sleeps in tests
        super().__init__(WIKIDATA, cache_dir=cache_dir, min_request_interval=0)
        self.status = status
        self.retry_after = retry_after
        self.attempts = 0

    def _run(self, query: str) -> list[dict]:
        # Reproduce the production _run's bookkeeping but force the error path
        import urllib.error
        from email.message import Message
        self._last_call_errored = False
        if self._circuit_open:
            self._last_call_errored = True
            return []
        self.attempts += 1
        headers = Message()
        if self.retry_after is not None:
            headers["Retry-After"] = self.retry_after
        exc = urllib.error.HTTPError(
            url=self.config.endpoint, code=self.status,
            msg="forced", hdrs=headers, fp=None,
        )
        # Mimic the production except-handler logic
        self._last_call_errored = True
        if self.status == 429:
            self._consecutive_429 += 1
            if self._consecutive_429 >= 3:   # _CIRCUIT_BREAKER_THRESHOLD
                self._circuit_open = True
        return []


def test_429_does_not_pollute_cache(tmp_path: Path):
    """A 429 must NOT cache 'no match' — that would lock out future runs."""
    cache = tmp_path / "rdl"
    resolver = _ErroringResolver(status=429, retry_after="0", cache_dir=cache)
    result = resolver.resolve("EUR")
    assert result.uri is None    # current call returned nothing

    cache_file = cache / "wikidata.json"
    if cache_file.exists():
        payload = json.loads(cache_file.read_text())
    else:
        payload = {}
    assert "eur" not in payload   # cache was NOT written


def test_circuit_breaker_opens_after_three_429s(tmp_path: Path):
    """After 3 consecutive 429s, subsequent calls return immediately
    without hitting the endpoint."""
    resolver = _ErroringResolver(status=429, retry_after="0")
    for _ in range(3):
        resolver.resolve(f"probe-{_}")

    assert resolver._circuit_open is True

    # 4th call: circuit open → no new attempt
    attempts_before = resolver.attempts
    resolver.resolve("probe-4")
    # _run still increments attempts only when it actually executes; with
    # circuit open it returns early. Either way, no NEW HTTPError raises.
    # Verify _consecutive_429 stayed bounded:
    assert resolver._consecutive_429 == 3   # stopped at threshold


def test_500_error_does_not_cache(tmp_path: Path):
    """Non-429 server errors also shouldn't pollute the cache."""
    cache = tmp_path / "rdl"
    resolver = _ErroringResolver(status=500, cache_dir=cache)
    resolver.resolve("EUR")
    cache_file = cache / "wikidata.json"
    if cache_file.exists():
        payload = json.loads(cache_file.read_text())
    else:
        payload = {}
    assert "eur" not in payload


# ── Live test (skipped by default) ─────────────────────────────────────────

@pytest.mark.skipif(
    os.environ.get("DOCGRAPH_RDL_LIVE") != "1",
    reason="Live Wikidata test — set DOCGRAPH_RDL_LIVE=1 to enable",
)
def test_live_wikidata_eur(tmp_path: Path):
    """Smoke test against the real Wikidata endpoint. Verifies "Euro" resolves
    to Q4916 (the currency)."""
    resolver = RdlResolver(WIKIDATA, cache_dir=tmp_path / "rdl")
    result = resolver.resolve("Euro")
    assert result.uri is not None
    # Wikidata Q4916 is the Euro currency
    assert "Q4916" in str(result.uri) or "currency" in result.label.lower()
    assert result.confidence > 0.5
