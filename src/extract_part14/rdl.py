"""External RDL (Reference Data Library) resolver — POC against Wikidata.

The walker's stage 2 extracts a probe phrase from the document (the LLM's
proposed value for a property like "currency" or "country"); the resolver
takes that phrase, hits an external SPARQL endpoint, and returns a URI for
the best matching entity.

POC choice: Wikidata. Public, reliable, no auth, well-labeled. The same
pattern works against POSC Caesar, IOGP, ECLASS, 15926.io — swap the
endpoint and the namespace, the resolver is unchanged.

Trade-offs vs the local-mirror approach (deferred for now):
  + No mirror/index to build or refresh
  + Always up-to-date with upstream
  + Trivial to add new RDLs (just configure endpoint URL)
  - Latency: ~200-500ms per resolution (HTTP round trip)
  - Failure mode: endpoint down → resolution fails (cached probes still work)
  - No fuzzy normalization (relies on endpoint-side label match)

Cache: per-probe results live in `.docgraph/cache/rdl/<rdl-name>.json`,
keyed by probe text. Means re-runs against the same document are free
unless the cache is dropped. Network errors fall back to cache.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from rdflib import URIRef

from src.log_panels import log_prompt, log_response

logger = logging.getLogger(__name__)


# Self-imposed rate limit (seconds between requests). Wikidata's stated
# robots policy is ≤ 2 RPS; 0.5s gives us a 30% safety margin.
_DEFAULT_MIN_REQUEST_INTERVAL = 0.5

# After this many consecutive 429s, give up on the endpoint for the
# remainder of the session — every subsequent probe returns "no match"
# instantly. The cache still keeps prior good resolutions usable.
_CIRCUIT_BREAKER_THRESHOLD = 3


@dataclass(frozen=True)
class RdlConfig:
    name:      str                # short slug for cache filename + logs
    endpoint:  str                # SPARQL endpoint URL
    namespace: str                # URI prefix that marks an entity as "from this RDL"
    label:     str = ""           # human-readable name for logs
    # Upper-ontology classes this RDL is competent for. Callers (enrich) skip
    # querying this RDL for entities whose type isn't (transitively) one of
    # these. Empty tuple = no constraint (universal scope, e.g. Wikidata).
    #
    # Example: POSC Caesar PLM-RDL covers industrial equipment + units + quantities,
    # NOT persons/organizations/locations/documents. Declaring `covers` here
    # saves a SPARQL round-trip for every Person, Organization, etc.
    covers:    tuple = ()


# LIS-14 namespace shorthand for `covers` declarations.
_LIS = "http://rds.posccaesar.org/ontology/lis14/rdl/"


# Default Wikidata POC config — public, no auth required.
# Best for value-resolution probes (currencies, countries, organizations,
# persons). Type semantics are loose (Wikidata is an instance graph, not
# a class hierarchy aligned with Part 14). Universal scope — no `covers`
# filter (Wikidata has something for almost any probe).
WIKIDATA = RdlConfig(
    name      = "wikidata",
    endpoint  = "https://query.wikidata.org/sparql",
    namespace = "http://www.wikidata.org/entity/",
    label     = "Wikidata",
    covers    = (),
)


# POSC Caesar PLM-RDL — the modern Part 14-aligned reference data library
# from POSC Caesar Association. Industrial-equipment / process-industry
# focused; classes are formally rdfs:subClassOf POSC Caesar's LIS-14
# vocabulary at http://rds.posccaesar.org/ontology/lis14/rdl/ .
#
# Apache Jena Fuseki backend; mixed-case labels (e.g. "Centrifugal Pump"),
# typically no language tag.
#
# *** Namespace alignment caveat ***
# POSC Caesar's lis: prefix is `http://rds.posccaesar.org/ontology/lis14/rdl/`
# whereas our bundled LIS-14.ttl uses `http://rds.posccaesar.org/ontology/lis14/rdl/`.
# Same conceptual ontology, two different URI sets. When this resolver
# adds `<my-entity> rdf:type pca:PCA_100004064` (subclass of POSC's
# lis:FunctionalObject), the inheritance chain DOES NOT transitively reach
# our `lis:FunctionalObject` without owl:equivalentClass alignments.
#
# Future fix: a small `vendor/ontologies/posccaesar-lis14-alignments.ttl`
# declaring `<our-iso15926-part14-uri> owl:equivalentClass <posc-lis14-uri>`
# for each top-level Part 14 class. Until then, pca: types are present in
# the graph but their LIS-14 ancestry is implicit (must be queried via the
# POSC Caesar URI side, not ours).
POSC_CAESAR = RdlConfig(
    name      = "posccaesar",
    endpoint  = "https://rds.posccaesar.org/ontology/fuseki/ontology/sparql",
    namespace = "http://rds.posccaesar.org/ontology/plm/rdl/",
    label     = "POSC Caesar PLM-RDL",
    # Industrial-equipment / process-industry scope.
    # NOTE: deliberately *narrower* than lis:PhysicalObject (which would
    # include lis:Organism → lis:Person → not POSC's domain). POSC PLM-RDL
    # covers:
    #  - inanimate physical things (equipment, vessels, instruments,
    #    substances, streams)
    #  - functional/system designations
    #  - units of measure + quantities
    #  - some industrial activities (welding, machining, ...)
    # It does NOT cover persons, organizations, locations, or generic
    # documents — those entities skip POSC entirely.
    covers = tuple(URIRef(_LIS + c) for c in (
        "FunctionalObject",
        "InanimatePhysicalObject",      # NOT PhysicalObject — too broad
        "Compound",
        "Stream",
        "UnitOfMeasure",
        "Quality",
        "PhysicalQuantity",
        "ScalarQuantityDatum",
        "Activity",
        # RealizableEntity covers Role + Disposition (Part 14 §E.6 BFO-style).
        # POSC may carry industrial role classes (operator, contractor, ...)
        # but won't have healthcare roles — type-hint probing still gives the
        # generic lis:Role a fair shot at refinement.
        "RealizableEntity",
    )),
)


# Legacy POSC Caesar RDS-WIP / EUR endpoint — flat reference data library
# at http://data.posccaesar.org/rdl/RDS<id>. Older/distinct from the
# modern PLM-RDL above. Not currently used; kept for reference.
POSC_CAESAR_RDS_WIP = RdlConfig(
    name      = "posccaesar_rds",
    endpoint  = "https://data.posccaesar.org/rdl/sparql",
    namespace = "http://data.posccaesar.org/rdl/",
    label     = "POSC Caesar RDS-WIP (legacy)",
)


@dataclass
class ResolutionResult:
    uri:        URIRef | None
    label:      str
    confidence: float


class RdlResolver:
    """Resolves natural-language probes against an external SPARQL endpoint."""

    def __init__(
        self,
        config: RdlConfig,
        cache_dir: Path | None = None,
        *,
        timeout: float = 10.0,
        user_agent: str = "docgraph/0.1 (+https://github.com/anthropics/claude-code)",
        min_request_interval: float = _DEFAULT_MIN_REQUEST_INTERVAL,
    ):
        self.config     = config
        self.timeout    = timeout
        self.user_agent = user_agent
        self.cache_dir  = cache_dir
        self._cache     = self._load_cache()
        self._min_interval     = min_request_interval
        self._last_request_at  = 0.0
        self._consecutive_429  = 0
        self._circuit_open     = False
        self._last_call_errored = False

    def resolve(self, probe: str, *, kind_hint: URIRef | None = None) -> ResolutionResult:
        """Resolve a probe phrase to an RDL URI.

        Returns ResolutionResult(uri=None, ...) when no acceptable match is
        found. The kind_hint (a class URI) is currently advisory — used to
        bias the SPARQL query but not required.
        """
        # Normalize whitespace so multi-line / tab / tabular probes don't break
        # the SPARQL string literal (and so cache hits collapse equivalent inputs).
        probe = " ".join(probe.split())
        if not probe:
            return ResolutionResult(uri=None, label="", confidence=0.0)

        cache_key = self._cache_key(probe, kind_hint)
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return ResolutionResult(
                uri        = URIRef(cached["uri"]) if cached.get("uri") else None,
                label      = cached.get("label", ""),
                confidence = float(cached.get("confidence", 0.0)),
            )

        # Step 1: exact label match
        result = self._sparql_exact(probe)
        exact_errored = self._last_call_errored

        # Step 2: fuzzy match if exact returned nothing AND wasn't errored
        # (no point asking again if the endpoint is down or rate-limiting).
        if result.uri is None and not exact_errored:
            result = self._sparql_fuzzy(probe, limit=5)
        any_errored = exact_errored or self._last_call_errored

        # Cache only successful (non-errored) responses. A rate-limit error
        # produces uri=None / confidence=0, but it'd be wrong to cache that
        # as "no match" — next run could succeed when the endpoint recovers.
        if not any_errored:
            self._cache[cache_key] = {
                "uri":        str(result.uri) if result.uri else None,
                "label":      result.label,
                "confidence": result.confidence,
            }
            self._save_cache()
        return result

    # ── SPARQL primitives ──────────────────────────────────────────────────

    def _sparql_exact(self, probe: str) -> ResolutionResult:
        """Exact case-insensitive match against rdfs:label OR skos:prefLabel
        OR skos:altLabel. UNION-form (NOT property-path alternation) so
        Fuseki/Virtuoso can use per-pattern indexes; the alternation form
        causes Fuseki to time out on large RDLs like POSC Caesar.

        Accepts labels with no language tag (POSC Caesar) or English
        (Wikidata) — covers most public RDLs.
        """
        probe_esc = _escape(probe)
        query = f"""\
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?item ?label WHERE {{
  {{ ?item rdfs:label ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{probe_esc}") && (LANG(?label) = "" || LANG(?label) = "en")) }}
  UNION
  {{ ?item skos:prefLabel ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{probe_esc}") && (LANG(?label) = "" || LANG(?label) = "en")) }}
  UNION
  {{ ?item skos:altLabel ?label .
    FILTER(LCASE(STR(?label)) = LCASE("{probe_esc}") && (LANG(?label) = "" || LANG(?label) = "en")) }}
}} LIMIT 1
"""
        rows = self._run(query)
        if not rows:
            return ResolutionResult(uri=None, label="", confidence=0.0)
        item = rows[0].get("item", {}).get("value")
        label = rows[0].get("label", {}).get("value", "")
        return ResolutionResult(
            uri=URIRef(item) if item else None,
            label=label,
            confidence=1.0,
        )

    def _sparql_fuzzy(self, probe: str, *, limit: int = 5) -> ResolutionResult:
        """Substring fuzzy match via FILTER CONTAINS, UNION-form.

        Caveat: FILTER CONTAINS is a full table scan and will be slow on
        large RDLs (POSC Caesar has tens of thousands of classes; this
        query may take many seconds). Endpoint-native FTS (Jena's
        `text:query`, Virtuoso's `bif:contains`, Wikidata's
        `wikibase:mwapi`) would be much faster but requires per-endpoint
        adapters. For the POC, accept the cost; per-RDL FTS plug-ins
        are a future improvement.
        """
        probe_esc = _escape(probe)
        query = f"""\
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?item ?label WHERE {{
  {{ ?item rdfs:label ?label .
    FILTER(CONTAINS(LCASE(STR(?label)), LCASE("{probe_esc}")) && (LANG(?label) = "" || LANG(?label) = "en")) }}
  UNION
  {{ ?item skos:prefLabel ?label .
    FILTER(CONTAINS(LCASE(STR(?label)), LCASE("{probe_esc}")) && (LANG(?label) = "" || LANG(?label) = "en")) }}
}} LIMIT {limit}
"""
        rows = self._run(query)
        if not rows:
            return ResolutionResult(uri=None, label="", confidence=0.0)
        first = rows[0]
        item  = first.get("item", {}).get("value")
        label = first.get("label", {}).get("value", "")
        confidence = 0.7 if label.lower() == probe.lower() else 0.6
        return ResolutionResult(
            uri=URIRef(item) if item else None,
            label=label,
            confidence=confidence,
        )

    def _run(self, query: str) -> list[dict]:
        """Execute a SPARQL SELECT and return the bindings list.

        Sets `self._last_call_errored` so callers can decide whether to
        cache the result. Honors a self-imposed rate limit, respects 429
        Retry-After hints, and opens a session-wide circuit breaker after
        repeated rate-limit failures.

        When the module's logger is at DEBUG, emits Rich panels for the
        outgoing SPARQL and the incoming bindings (same panel shape as the
        LLM prompt/response panels — visible under `--debug`).
        """
        self._last_call_errored = False
        # SPARQL panel kind (exact vs fuzzy) is inferable from query body
        kind = "fuzzy" if "CONTAINS" in query else "exact"
        stage = f"rdl/{self.config.name}/{kind}"
        meta = f"{self.config.endpoint}"
        log_prompt(stage, query, logger=logger, metadata=meta)

        if self._circuit_open:
            log_response(stage, "(circuit breaker open — request skipped)",
                         logger=logger, metadata=meta)
            self._last_call_errored = True
            return []

        # Self-imposed rate limit
        now = time.monotonic()
        wait_for_interval = self._min_interval - (now - self._last_request_at)
        if wait_for_interval > 0:
            time.sleep(wait_for_interval)

        url = self.config.endpoint + "?" + urllib.parse.urlencode({
            "query":  query,
            "format": "json",
        })
        req = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept":     "application/sparql-results+json",
        })

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                payload = json.loads(raw)
            self._last_request_at  = time.monotonic()
            self._consecutive_429  = 0
            bindings = payload.get("results", {}).get("bindings", []) or []
            log_response(stage, raw, logger=logger, metadata=meta, as_json=True)
            return bindings
        except urllib.error.HTTPError as exc:
            self._last_request_at = time.monotonic()
            self._last_call_errored = True
            err_msg = f"HTTP {exc.code}: {exc.reason}"
            if exc.code == 429:
                self._consecutive_429 += 1
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait_secs = (
                    int(retry_after) if retry_after and retry_after.isdigit() else 60
                )
                err_msg += f"  (Retry-After={wait_secs}s, consecutive 429s={self._consecutive_429})"
                logger.warning(
                    "RDL %s: 429 rate-limit (#%d); honoring Retry-After=%ds",
                    self.config.name, self._consecutive_429, wait_secs,
                )
                if self._consecutive_429 >= _CIRCUIT_BREAKER_THRESHOLD:
                    logger.warning(
                        "RDL %s: %d consecutive 429s — opening circuit breaker; "
                        "no further requests this session",
                        self.config.name, self._consecutive_429,
                    )
                    self._circuit_open = True
                else:
                    time.sleep(wait_secs)
                log_response(stage, err_msg, logger=logger, metadata=meta)
                return []
            logger.warning("RDL %s: SPARQL request failed (%s)", self.config.name, exc)
            log_response(stage, err_msg, logger=logger, metadata=meta)
            return []
        except Exception as exc:
            self._last_request_at = time.monotonic()
            self._last_call_errored = True
            logger.warning("RDL %s: SPARQL request failed (%s)", self.config.name, exc)
            log_response(stage, f"(request failed) {exc}", logger=logger, metadata=meta)
            return []

    # ── Cache ──────────────────────────────────────────────────────────────

    def _cache_key(self, probe: str, kind_hint: URIRef | None) -> str:
        if kind_hint is None:
            return probe.lower().strip()
        return f"{kind_hint}|{probe.lower().strip()}"

    def _cache_path(self) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{self.config.name}.json"

    def _load_cache(self) -> dict:
        path = self._cache_path()
        if path is None or not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("RDL %s: cache load failed (%s)", self.config.name, exc)
            return {}

    def _save_cache(self) -> None:
        path = self._cache_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(json.dumps(self._cache, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except Exception as exc:
            logger.warning("RDL %s: cache save failed (%s)", self.config.name, exc)


def _escape(s: str) -> str:
    """Sanitize + escape a string for safe inclusion in a SPARQL string literal.

    Per SPARQL 1.1 string-literal syntax (https://www.w3.org/TR/sparql11-query/#rString),
    the only chars that may appear unescaped inside double-quoted literals are
    everything EXCEPT `"`, `\\`, `\n`, `\r`. We:
      1. Collapse all whitespace (tabs/newlines/etc.) to single spaces — keeps
         the probe on a single logical line and avoids the most common 400s.
      2. Escape backslash and double-quote.
      3. Strip control characters (anything below 0x20 after step 1).
      4. Cap length defensively (queries with very long FILTER args reliably
         break some endpoints).
    """
    # 1. Collapse whitespace
    s = " ".join(s.split())
    # 3. Drop residual control chars (after split, only U+0020 and printable remain)
    s = "".join(c for c in s if c >= " " or c == " ")
    # 2. Escape backslash first, then double-quote
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    # 4. Cap (POSC's endpoint specifically complains on very long FILTER strings)
    if len(s) > 200:
        s = s[:200]
    return s
