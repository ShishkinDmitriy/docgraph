"""Document extraction agent.

Phases per document:
  1. get_extraction_plan  — code reads shape, returns template + secondary list
  2. LLM fills template, calls find_entity for secondary stubs
  3. validate             — code runs SHACL, returns violations (max 2 calls)
  4. submit_extraction    — LLM submits final document
"""

import json
import logging

from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import RDF as RDF_NS, RDFS
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

from .tool.find_entity_tool import FIND_ENTITY_TOOL, find_entity
from .tool.get_shape_tool import GET_SHAPE_TOOL, get_shape
from .tool.submit_extraction_tool import SUBMIT_EXTRACTION_TOOL
from .tool.validate_tool import VALIDATE_TOOL, validate_tool

from .llm import LLMClient
from .models import ClassificationResult, DocumentClass, DocumentHit, ModelConfig
from . import ontology as _ontology
from .ontology import JSONLD_CONTEXT, prefixed_name

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
3. Scan the template for nested objects that have "<RESOLVE>" @id placeholders and
   that carry strong identifiers in the document — e.g. a tax ID, registration
   number, or account number, not just a name.
   For each such object:
   a. Call get_shape(child_class) to learn its properties.
      If get_shape returns an error (no shape defined), use only the properties
      already present in the parent template for that object — do not invent fields.
   b. Extract the strong identifiers from the document using the shape as a guide.
   c. Call find_entity(child_class, {those identifiers}) and record the resolved URI.
   Rationale: resolving a child entity first lets you use its stable URI — rather
   than a weak name string — when searching for the root entity in step 4.
4. Using the template as a guide, pick the most uniquely identifying fields from
   the document (e.g. document number, total amount, date). Where step 3 resolved
   a child, pass its URI as the property value instead of the raw string.
   Call find_entity(class_uri, {those fields}).
   - Match found → call submit_extraction({"@id": match_uri, "@type": class_uri}).
     Document already exists — done.
   - No match → use suggested_uri as the root @id and proceed to step 5.
5. Fill every placeholder in the template with values from the document.
   Set the root "@id" to the suggested_uri from step 4.
   Use the URIs already resolved in step 3 for those child objects.
6. REQUIRED — resolve every "@id" that still starts with "<RESOLVE":
   - The @type shows the concrete class (or "one of: A | B") — pick from document content.
   - Call find_entity(concrete_class, {most identifying fields}).
   - Use matched URI or suggested_uri as @id (plain string, no angle brackets).
   - Include document properties absent from or different to known_properties
     (new facts are merged; a different address is not an error — both values kept).
   Do NOT submit while any "@id" value starts with "<RESOLVE".
7. Call validate(document) — fix violations if any, at most twice.
8. Call submit_extraction(document).

Extraction rules:
- Replace every placeholder with a value from the document; use null if absent.
- Dates → {"@value": "YYYY-MM-DD", "@type": "xsd:date"}
- Decimals → plain JSON number
- Arrays: repeat the item pattern for every occurrence; increment trailing index on @id
- Do NOT add @context
- Do NOT add properties that are not in the template — only fill what the shape defines.
"""

AGENT_TOOLS: list[dict] = [
    GET_SHAPE_TOOL,
    FIND_ENTITY_TOOL,
    VALIDATE_TOOL,
    SUBMIT_EXTRACTION_TOOL,
]

class DocumentAgent:
    MAX_TURNS = 16

    def __init__(
        self,
        graph: Graph,                       # combined ontology graph (shapes + ontologies)
        results_graph: Graph,               # accumulated results — used by find_entity
        client: LLMClient,
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

        for turn in range(self.MAX_TURNS):
            _log_request(messages, turn)
            response = self.client.create(
                model_id=self.model.model_id,
                max_tokens=4096,
                system=_SYSTEM,
                tools=AGENT_TOOLS,
                messages=messages,
            )
            _log_response(response, turn)
            messages.append({"role": "assistant", "content": response.assistant_message})
            if response.stop_reason in ("end_turn", "max_tokens"):
                break
            if response.stop_reason == "tool_use":
                tool_results = []
                done = False
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    result, is_terminal = self._dispatch(block.name, block.input)
                    _log_tool_responses(result, block)
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

    def _dispatch(self, name: str, inp: dict) -> tuple[dict, bool]:
        if name == "get_shape":
            return get_shape(self, 
                inp["class_uri"],
                confidence=inp.get("confidence"),
                reason=inp.get("reason"),
            ), False
        if name == "find_entity":
            return find_entity(self, inp["class_uri"], inp.get("properties", {})), False
        if name == "validate":
            return validate_tool(self, inp["document"]), False
        if name == "submit_extraction":
            self._submitted = inp["document"]
            return {"status": "accepted"}, True
        return {"error": f"unknown tool: {name}"}, False

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

def _log_request(messages: list[dict], turn: int):
    """Log the last outgoing message as a readable string for debug panels."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    # Show the last outgoing message (prompt on turn 0, tool results on later turns)
    last = messages[-1]
    parts = []
    content = last.get("content", [])
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
    last_text = "\n\n".join(parts) if parts else str(content)
    _console.print(Panel(
        last_text,
        title=f"[bold cyan]agent → LLM[/bold cyan]  turn {turn}",
        border_style="cyan",
        title_align="left",
    ))

def _log_response(response, turn: int):
    """Log an API response as a readable string for debug panels."""
    if not logger.isEnabledFor(logging.DEBUG):
        return
    parts = []
    for block in response.content:
        if block.type == "text":
            parts.append(block.text)
        elif block.type == "tool_use":
            args = json.dumps(block.input, indent=2, ensure_ascii=False)
            parts.append(f"tool_use: {block.name}\n{args}")
    summary = "\n\n".join(parts) if parts else "(empty)"
    _console.print(Panel(
        summary,
        title=f"[bold green]LLM → agent[/bold green]  turn {turn}  stop={response.stop_reason}",
        border_style="green",
        title_align="left",
    ))

def _log_tool_responses(result, block):
    if not logger.isEnabledFor(logging.DEBUG):
        return
    _console.print(Panel(
        Syntax(json.dumps(result, indent=2, ensure_ascii=False), "json", theme="monokai"),
        title=f"[bold yellow]tool result[/bold yellow]  {block.name}",
        border_style="yellow",
        title_align="left",
    ))

def run_extraction(
    content_block: dict,
    graph: Graph,
    results_graph: Graph,
    doc_classes: dict,
    target_class: URIRef,
    client: LLMClient,
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
