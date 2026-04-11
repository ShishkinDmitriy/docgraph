"""Document extraction agent.

Phases per document:
  1. get_extraction_plan  — code reads shape, returns template + secondary list
  2. LLM fills template, calls find_entity for secondary stubs
  3. validate             — code runs SHACL, returns violations (max 2 calls)
  4. submit_extraction    — LLM submits final document
"""

import json
import logging

import anthropic
from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import RDF as RDF_NS, RDFS
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from .agent_tools import AGENT_TOOLS
from .models import ClassificationResult, DocumentClass, DocumentHit, ModelConfig
from . import ontology as _ontology
from .ontology import JSONLD_CONTEXT, prefixed_name
from .shape_extractor import (
    find_extraction_shape,
    shape_to_template,
    _slug,
)

logger = logging.getLogger(__name__)
_console = Console(stderr=True)

SH  = Namespace("http://www.w3.org/ns/shacl#")
TAX = Namespace("http://example.org/tax-classifier/")

_SYSTEM = """\
You are a financial document extractor working with RDF/JSON-LD.

Convention: values inside <> are instructions/placeholders to be resolved.
Resolved values are plain strings without angle brackets.

Convention for placeholders: both "@id" and "@type" use "<RESOLVE...>" to signal
that the value must be determined from the document.
  "@id": "<RESOLVE>"                            → look up or mint a URI
  "@type": "<RESOLVE:Base — one of: A | B>"    → pick the concrete class
  "@type": "fin:DemandForPayment"              → already resolved, use as-is

Steps:
1. Read the document. Determine the concrete class from the @type hint.
2. Call get_shape(class_uri, confidence, reason).
   The returned template shows all properties; the root @id is still "<RESOLVE>".
3. Using the template as a guide, pick the most uniquely identifying fields from
   the document (e.g. document number, total amount, date).
   Call find_entity(class_uri, {those fields}).
   - Match found → call submit_extraction({"@id": match_uri, "@type": class_uri}).
     Document already exists — done.
   - No match → use suggested_uri as the root @id and proceed to step 4.
4. Fill every placeholder in the template with values from the document.
   Set the root "@id" to the suggested_uri from step 3.
5. REQUIRED — resolve every "@id" that still starts with "<RESOLVE":
   - The @type shows the concrete class (or "one of: A | B") — pick from document content.
   - Call find_entity(concrete_class, {most identifying fields}).
   - Use matched URI or suggested_uri as @id (plain string, no angle brackets).
   - Include document properties absent from or different to known_properties
     (new facts are merged; a different address is not an error — both values kept).
   Do NOT submit while any "@id" value starts with "<RESOLVE".
6. Call validate(document) — fix violations if any, at most twice.
7. Call submit_extraction(document).

Extraction rules:
- Replace every placeholder with a value from the document; use null if absent.
- Dates → {"@value": "YYYY-MM-DD", "@type": "xsd:date"}
- Decimals → plain JSON number
- Arrays: repeat the item pattern for every occurrence; increment trailing index on @id
- Do NOT add @context
- Do NOT add properties that are not in the template — only fill what the shape defines.
"""



class DocumentAgent:
    MAX_TURNS = 16

    def __init__(
        self,
        graph: Graph,                       # combined ontology graph (shapes + ontologies)
        results_graph: Graph,               # accumulated results — used by find_entity
        client: anthropic.Anthropic,
        model: ModelConfig,
        doc_classes: dict[str, DocumentClass],
        target_class: URIRef,               # root class, e.g. fin:FinancialDocument
    ):
        self.graph = graph
        self.results_graph = results_graph
        self.client = client
        self.model = model
        self.doc_classes = doc_classes
        self.target_class = target_class
        # reverse map: full URI string → notation
        self._uri_to_notation: dict[str, str] = {
            str(cls.uri): notation for notation, cls in doc_classes.items()
        }

    def run(
        self,
        content_block: dict,
        note: str | None = None,
        on_classified=None,
    ) -> DocumentHit | None:
        """
        Classify, deduplicate, and extract a document in a single agent loop.
        Returns a DocumentHit (with .details filled) or None.
        """
        self._validate_count = 0
        self._submitted: dict | None = None
        self._hit: DocumentHit | None = None
        self._on_classified = on_classified

        # Root stub — identical RESOLVE pattern used for all secondary objects.
        # @id is just <RESOLVE> — no type encoded there.
        # @type carries the base class and the concrete options as a hierarchy tree.
        base_curie = prefixed_name(self.target_class)
        by_uri = {str(cls.uri): cls for cls in self.doc_classes.values()}
        tree_lines = _build_type_tree(self.graph, self.target_class, by_uri)
        type_hint = "\n" + "\n".join(tree_lines)
        root_stub = {
            "@id":   "<RESOLVE>",
            "@type": f"<RESOLVE:{base_curie} — pick the most specific matching class:{type_hint}>",
        }

        prompt = (
            f"Document stub to classify and resolve:\n"
            f"{json.dumps(root_stub, indent=2, ensure_ascii=False)}\n\n"
            f"Follow the steps in the system prompt: call get_shape first."
        )
        if note:
            prompt += f"\n\nUser note: {note}"

        messages: list[dict] = [
            {"role": "user", "content": [content_block, {"type": "text", "text": prompt}]}
        ]

        _dbg = logger.isEnabledFor(logging.DEBUG)

        for turn in range(self.MAX_TURNS):
            if _dbg:
                # Show the last outgoing message (prompt on turn 0, tool results on later turns)
                last = messages[-1]
                last_text = _summarise_message(last)
                _console.print(Panel(
                    last_text,
                    title=f"[bold cyan]agent → LLM[/bold cyan]  turn {turn}",
                    border_style="cyan",
                    title_align="left",
                ))

            response = self.client.messages.create(
                model=self.model.model_id,
                max_tokens=4096,
                system=_SYSTEM,
                tools=AGENT_TOOLS,
                messages=messages,
            )

            if _dbg:
                _console.print(Panel(
                    _summarise_response(response),
                    title=f"[bold green]LLM → agent[/bold green]  turn {turn}  stop={response.stop_reason}",
                    border_style="green",
                    title_align="left",
                ))

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason in ("end_turn", "max_tokens"):
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                done = False
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result, is_terminal = self._dispatch(block.name, block.input)
                    if _dbg:
                        _console.print(Panel(
                            Syntax(json.dumps(result, indent=2, ensure_ascii=False), "json", theme="monokai"),
                            title=f"[bold yellow]tool result[/bold yellow]  {block.name}",
                            border_style="yellow",
                            title_align="left",
                        ))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                    if is_terminal:
                        done = True
                messages.append({"role": "user", "content": tool_results})
                if done:
                    break

        if self._hit and self._submitted:
            self._hit.details = self._submitted
        return self._hit

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    def _dispatch(self, name: str, inp: dict) -> tuple[dict, bool]:
        if name == "get_shape":
            return self._get_shape(
                inp["class_uri"],
                confidence=inp.get("confidence"),
                reason=inp.get("reason"),
            ), False
        if name == "find_entity":
            return self._find_entity(inp["class_uri"], inp.get("properties", {})), False
        if name == "validate":
            return self._validate(inp["document"]), False
        if name == "submit_extraction":
            self._submitted = inp["document"]
            return {"status": "accepted"}, True
        return {"error": f"unknown tool: {name}"}, False

    # ── Tool implementations ──────────────────────────────────────────────────

    def _get_shape(
        self,
        class_uri: str,
        confidence: float | None = None,
        reason: str | None = None,
    ) -> dict:
        notation = self._uri_to_notation.get(class_uri)
        if notation is None:
            return {"error": f"unknown class URI: {class_uri} — must be one of the URIs listed in @type"}

        # Record the classification hit (may already exist from _find_entity).
        if self._hit is None:
            self._hit = DocumentHit(
                category=notation,
                class_uri=class_uri,
                confidence=float(confidence) if confidence is not None else 1.0,
                reason=reason or "",
            )
            if self._on_classified:
                self._on_classified(self._hit)
        elif confidence is not None:
            self._hit.confidence = float(confidence)
            self._hit.reason = reason or self._hit.reason
        logger.info("agent | classified: %s (%.0f%%)", notation,
                    self._hit.confidence * 100)

        shape_uri = find_extraction_shape(self.graph, class_uri)
        if shape_uri is None:
            return {"error": f"no extraction shape for {class_uri}"}

        # Use the class URI as node_uri placeholder — the root @id will be RESOLVE:
        # so the LLM fills it with the URI returned by find_entity.
        # All nested objects are either inline (no @id) or secondary (RESOLVE: already),
        # so the placeholder value is never visible in the final output.
        template = shape_to_template(
            self.graph, self.graph, shape_uri, class_uri,
            type_override=class_uri,
        )
        # Root @id: RESOLVE — the LLM fills it with suggested_uri from find_entity.
        template["@id"] = "<RESOLVE>"

        logger.debug("agent | shape: %d template keys", len(template))
        return {"template": template}

    def _find_entity(self, class_uri: str, properties: dict) -> dict:
        expanded_class = _expand(class_uri)

        # If this is a document class (notation in our map), record the classification hit.
        notation = self._uri_to_notation.get(expanded_class) or self._uri_to_notation.get(class_uri)
        if notation and self._hit is None:
            self._hit = DocumentHit(
                category=notation,
                class_uri=expanded_class,
                confidence=0.0,   # will be updated by get_extraction_plan
                reason="",
            )
            if self._on_classified:
                self._on_classified(self._hit)

        # Build a stable suggested URI in the configured output namespace.
        class_local = _slug(class_uri.rsplit(":", 1)[-1].rsplit("/", 1)[-1].lower())
        name_value = (
            properties.get("foaf:name")
            or properties.get("fin:taxId")
            or properties.get("fin:registrationNumber")
            or next((v for v in properties.values() if isinstance(v, str) and v), None)
            or class_local
        )
        local = f"{class_local}_{_slug(str(name_value).lower())}"
        prefix = _ontology.OUTPUT_PREFIX
        suggested_uri = f"{prefix}:{local}" if prefix else str(_ontology.OUTPUT_NS[local])

        # Expand abstract superclasses to their known concrete subclasses so a
        # query for foaf:Agent also matches foaf:Person and foaf:Organization.
        concrete = {expanded_class}
        for sub in self.graph.subjects(RDFS.subClassOf, URIRef(expanded_class)):
            concrete.add(str(sub))
        if len(concrete) == 1:
            type_clause = f"  ?s a {_sparql_term(URIRef(expanded_class))} ."
        else:
            values = " ".join(_sparql_term(URIRef(c)) for c in concrete)
            type_clause = f"  ?s a ?_type . VALUES ?_type {{ {values} }}"
        clauses = [type_clause]
        var_counter = 0
        for prop, value in properties.items():
            if value is None or value == "null" or value == "":
                continue
            inline, filter_tmpl = _sparql_literal(value)
            if inline is None and filter_tmpl is None:
                continue
            segments = [seg.strip() for seg in prop.split("/")]

            if filter_tmpl is not None:
                # Typed value: bind to intermediate variable, then FILTER.
                # Multi-hop paths also need the intermediate chain below.
                subject = "?s"
                for seg in segments[:-1]:
                    nxt = f"?_v{var_counter}"
                    var_counter += 1
                    clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(seg)))} {nxt} .")
                    subject = nxt
                leaf_var = f"?_v{var_counter}"
                var_counter += 1
                clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(segments[-1])))} {leaf_var} .")
                clauses.append(f"  FILTER({filter_tmpl.format(i=leaf_var[1:])})")
            elif len(segments) == 1:
                clauses.append(f"  ?s {_sparql_term(URIRef(_expand(segments[0])))} {inline} .")
            else:
                # Multi-hop path: explicit intermediate variables.
                subject = "?s"
                for seg in segments[:-1]:
                    nxt = f"?_v{var_counter}"
                    var_counter += 1
                    clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(seg)))} {nxt} .")
                    subject = nxt
                clauses.append(f"  {subject} {_sparql_term(URIRef(_expand(segments[-1])))} {inline} .")

        query = _sparql_prefixes() + "SELECT ?s WHERE {\n" + "\n".join(clauses) + "\n}"

        # Query both the accumulated results and the ontology graph (which may
        # contain pre-declared known entities like persons or organisations).
        found: set[str] = set()
        for g in (self.results_graph, self.graph):
            if len(g) == 0:
                continue
            try:
                found.update(str(r.s) for r in g.query(query))
            except Exception as exc:
                logger.warning("agent | find_entity query failed on graph: %s", exc)

        # For each matched URI, collect its known properties from both graphs
        # so the LLM can compare them with what the current document says.
        matches = []
        for uri in found:
            known: dict[str, list] = {}
            for g in (self.graph, self.results_graph):
                for pred, obj in g.predicate_objects(URIRef(uri)):
                    key = prefixed_name(pred)
                    val = str(obj)
                    known.setdefault(key, [])
                    if val not in known[key]:
                        known[key].append(val)
            # Flatten single-value lists for readability
            flat = {k: (v[0] if len(v) == 1 else v) for k, v in known.items()}
            matches.append({"uri": uri, "known_properties": flat})

        logger.debug(
            "agent | find_entity %s props=%s → %d match(es)\nquery:\n%s",
            class_uri, properties, len(matches), query,
        )
        return {"matches": matches, "suggested_uri": suggested_uri}

    def _validate(self, document: dict) -> dict:
        if self._validate_count >= 2:
            return {"violations": [], "note": "max attempts reached"}
        self._validate_count += 1

        data_graph = Graph()
        jsonld = {**document, "@context": JSONLD_CONTEXT}
        try:
            data_graph.parse(
                data=json.dumps(jsonld, ensure_ascii=False), format="json-ld"
            )
        except Exception as exc:
            return {"violations": [{"message": f"parse error: {exc}"}]}

        try:
            from pyshacl import validate as shacl_validate
            from rdflib.namespace import SH as SH_NS

            conforms, results_graph, _ = shacl_validate(
                data_graph,
                shacl_graph=self.graph,
                inference="none",
                abort_on_first=False,
            )
            if conforms:
                return {"violations": []}

            violations = []
            for node in results_graph.subjects(RDF_NS.type, SH_NS.ValidationResult):
                path     = results_graph.value(node, SH_NS.resultPath)
                message  = results_graph.value(node, SH_NS.resultMessage)
                severity = results_graph.value(node, SH_NS.resultSeverity)
                violations.append({
                    "path":     str(path).rsplit("#", 1)[-1] if path else None,
                    "message":  str(message) if message else "constraint violated",
                    "severity": str(severity).rsplit("#", 1)[-1] if severity else "Violation",
                })
            logger.debug("agent | validate: %d violation(s)", len(violations))
            return {"violations": violations}
        except ImportError:
            return {"violations": [], "note": "pyshacl not installed"}
        except Exception as exc:
            logger.warning("agent | validate error: %s", exc)
            return {"violations": [], "error": str(exc)}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_type_tree(
    graph: Graph,
    root: URIRef,
    by_uri: dict,           # str(uri) → DocumentClass
    depth: int = 0,
) -> list[str]:
    """
    Recursively render the class hierarchy below `root` as indented text lines.

    Concrete classes (with skos:notation / in by_uri) appear as leaf lines:
        fin:DemandForPayment [http://...] — definition text

    Intermediate classes (abstract, not in by_uri) appear as group headers:
        fin:Transaction (abstract)
    and are skipped when they have no concrete descendants.
    """
    pad = "  " * depth
    lines = []
    direct_subs = sorted(
        s for s in graph.subjects(RDFS.subClassOf, root)
        if isinstance(s, URIRef)
    )
    for sub in direct_subs:
        sub_str = str(sub)
        curie = prefixed_name(sub)
        if sub_str in by_uri:
            dc = by_uri[sub_str]
            lines.append(f"{pad}{curie} [{sub_str}] — {dc.definition}")
        else:
            children = _build_type_tree(graph, sub, by_uri, depth + 1)
            if children:
                lines.append(f"{pad}{curie} (abstract)")
                lines.extend(children)
    return lines


def _sparql_prefixes() -> str:
    """Return SPARQL PREFIX declarations for all known namespaces."""
    return "\n".join(
        f"PREFIX {prefix}: <{ns}>"
        for prefix, ns in JSONLD_CONTEXT.items()
    ) + "\n"


def _summarise_message(msg: dict) -> str:
    """Render the last outgoing message as a readable string for debug panels."""
    parts = []
    content = msg.get("content", [])
    if isinstance(content, str):
        return content
    for block in content:
        if isinstance(block, dict):
            t = block.get("type", "")
            if t == "text":
                parts.append(block["text"])
            elif t == "tool_result":
                parts.append(f"[tool_result id={block.get('tool_use_id', '?')}]\n{block.get('content', '')}")
            elif t == "document":
                parts.append("[cached document block]")
        elif hasattr(block, "type"):
            if block.type == "text":
                parts.append(block.text)
            elif block.type == "tool_result":
                parts.append(f"[tool_result]\n{block.content}")
    return "\n\n".join(parts) if parts else str(content)


def _summarise_response(response) -> str:
    """Render an API response as a readable string for debug panels."""
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
        elif block.type == "tool_use":
            args = json.dumps(block.input, indent=2, ensure_ascii=False)
            parts.append(f"tool_use: {block.name}\n{args}")
    return "\n\n".join(parts) if parts else "(empty)"


import re as _re
_DATE_RE  = _re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GYEAR_RE = _re.compile(r"^\d{4}$")


def _sparql_literal(value) -> tuple[str | None, str | None]:
    """
    Return (inline_literal, filter_expr) for a SPARQL clause, or (None, None) to skip.

    inline_literal  — used directly in the triple pattern:  ?s prop "value" .
    filter_expr     — requires an intermediate variable:
                        ?s prop ?_vN . FILTER(filter_expr(?_vN))

    Typed literals (dates, years) use FILTER(str(?var) = "...") because
    rdflib's JSON-LD parser may store xsd:date as a CURIE instead of a full
    URI, so direct typed-literal matching is unreliable.
    """
    if isinstance(value, bool):
        return ("true" if value else "false", None)
    if isinstance(value, (int, float)):
        return (str(value), None)
    if isinstance(value, str):
        safe = value.replace("\\", "\\\\").replace('"', '\\"')
        if _DATE_RE.match(value) or _GYEAR_RE.match(value):
            return (None, f'str(?_v{{i}}) = "{safe}"')
        return (f'"{safe}"', None)
    return (None, None)


def _sparql_term(uri: URIRef) -> str:
    """
    Return a CURIE if the prefix is declared in JSONLD_CONTEXT, else <full-uri>.
    Using a CURIE requires the matching PREFIX declaration in the query header;
    falling back to <uri> syntax works without any PREFIX declarations.
    """
    name = prefixed_name(uri)
    if ":" not in name or name.startswith("http") or name.startswith("urn"):
        return f"<{uri}>"
    return name



def _expand(curie: str) -> str:
    """Expand a CURIE to a full URI using JSONLD_CONTEXT."""
    if curie.startswith(("http", "urn")):
        return curie
    for prefix, ns in JSONLD_CONTEXT.items():
        if curie.startswith(f"{prefix}:"):
            return ns + curie[len(prefix) + 1:]
    return curie


# ── Public pipeline entry point ───────────────────────────────────────────────

def run_extraction(
    content_block: dict,
    graph: Graph,
    results_graph: Graph,
    doc_classes: dict,
    target_class: URIRef,
    client: anthropic.Anthropic,
    model: ModelConfig,
    note: str | None = None,
    on_hit_classified=None,
    on_hit_extracted=None,
) -> ClassificationResult:
    """
    Single-pass pipeline: classify, deduplicate, and extract in one agent loop.
    The agent determines the document URI via find_entity (no pre-minting needed).
    """
    agent = DocumentAgent(graph, results_graph, client, model, doc_classes, target_class)
    hit = agent.run(
        content_block, note=note,
        on_classified=on_hit_classified,
    )

    hits = [hit] if hit else []
    if not hit:
        logger.warning("agent | no classification/extraction returned")

    if on_hit_extracted and hit:
        on_hit_extracted(hit)

    return ClassificationResult(documents=hits)
