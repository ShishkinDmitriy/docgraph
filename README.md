# docgraph

Classify PDF documents and extract structured RDF data using Claude.

The project is configured through a small ontology registry (`data/docgraph.ttl`) that declares what ontologies to load, which OWL class is the classification target, which Claude model to use, and where to write results. Everything else — document categories, SHACL shapes, entity vocabularies — lives in plain Turtle files.

## How it works

1. **Load registry** — `data/docgraph.ttl` is parsed. It lists local ontology files to merge and optional remote ontologies (FOAF, SKOS, PROV-O) to fetch.
2. **Convert PDF to Markdown** — each PDF is rendered to Markdown via Claude Vision and cached alongside the PDF.
3. **Agent loop** — a Claude agent classifies the document, deduplicates entities against previously extracted results (via SPARQL), and extracts structured properties as JSON-LD.
4. **Persist** — results are appended to `classified/results.ttl` as RDF triples in the configured output namespace.
5. **Validate** — the output graph is checked against SHACL shapes.

## Setup

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
```

## Usage

```
python main.py <input.pdf|directory/>
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--docgraph` | `data/docgraph.ttl` | Project registry ontology |
| `--min-confidence` | `0.5` | Skip hits below this threshold |
| `--force` / `-f` | off | Re-classify already-processed files |
| `--dry-run` / `-n` | off | Preview without writing results |
| `--offline` | off | Skip fetching remote ontologies listed in docgraph.ttl |
| `--note` | — | Free-text hint passed to the classifier |
| `--debug` | off | Print full prompts and LLM responses |

Results are written to the path declared in `docgraph:results` (default: `classified/results.ttl`).

## Project layout

```
data/
  docgraph.ttl           # project registry — start here
  financial_documents.ttl  # OWL classes for fin: document types
  shapes.ttl             # SHACL validation rules
  models.ttl             # LLM model declarations
classified/
  results.ttl            # extracted RDF output (gitignored)
src/classifier/
  agent.py               # Claude agent: classify → deduplicate → extract
  ontology.py            # registry loader, JSONLD_CONTEXT, namespace utils
  classifier.py          # PDF → Markdown content block
  validator.py           # SHACL validation wrapper
  results.py             # append/query results.ttl
  markdown_io.py         # Markdown cache read/write
main.py                  # CLI entry point
```

## Configuring document classes

Add an OWL class to one of the local ontology files (or a new one declared in `docgraph.ttl`). A class is picked up as a classification target when it:

- is a transitive subclass of `docgraph:targetClass` (default: `foaf:Document`)
- carries `skos:notation` (used as the category key)
- carries `skos:definition` (shown to the model)

```turtle
fin:Invoice a owl:Class, skos:Concept ;
    rdfs:subClassOf fin:DemandForPayment ;
    skos:notation   "invoice" ;
    skos:definition "A B2B demand for payment issued after goods or services were delivered." .
```

## Configuring the output namespace

Edit `docgraph:results` in `data/docgraph.ttl`:

```turtle
docgraph:results a docgraph:Output ;
    docgraph:prefix       "result" ;
    docgraph:relativePath "classified/results.ttl" ;
    docgraph:namespace    "http://example.org/result/" .
```

Minted entity URIs will use the declared namespace and prefix (e.g. `result:person_alice`).

## Future plans

**Model abstraction** — the agent is currently coupled to the Anthropic SDK. The goal is to introduce a thin model interface so any backend can be plugged in: OpenAI-compatible APIs, Ollama, llama.cpp, or any local model that speaks a standard protocol. The ontology would declare the model type alongside the model ID, and the agent would receive a generic client rather than an `anthropic.Anthropic` instance.

**Named graphs as containers** — today all extracted triples land in a flat `results.ttl`. The plan is to make each extracted document a named graph (RDF dataset, TriG format), so a single file holds many documents with clean boundaries. This enables selective regeneration (drop one graph and re-extract without touching others), easier diffing, and provenance queries scoped to a single source document.

Confidence and provenance attach to the named graph, not to individual entities. Classification confidence (how sure the LLM is this is an Invoice vs. a Quote) is one float on the graph. Entity resolution method (was `result:org_acme` found by taxId or by name?) is recorded in the graph's provenance activity — it describes the act of extraction, not the entity itself, so `result:org_acme` stays clean. When the same entity appears in multiple graphs with conflicting property values (e.g. a full address in graph A vs. a scan-truncated fragment in graph B), the merge step picks the value from the graph with the stronger resolution signal. Illegible values (`[UNCLEAR...]` markers from OCR) are treated as null at extraction time rather than stored as fragments — a missing value is more useful than a wrong one.

**Results as a persistent RDF dataset** — currently `--force` deletes `results.ttl` and starts over. Instead, the results file should be loaded at startup as a proper RDF dataset and treated as persistent storage across runs. Each named graph is linked to its source input file, so `--force` on a specific PDF simply drops and replaces that graph rather than wiping everything. New extractions are merged in; unchanged documents are left untouched. This also means the dataset accumulates knowledge across many runs and can be queried incrementally without full re-extraction.

**Ontology bundles** — instead of listing individual ontology files in `docgraph.ttl`, a bundle would package a coherent set of classes, shapes, and namespace declarations together under a single identifier. You would point `docgraph:hasOntology` at a bundle rather than individual files. Bundles could be versioned, shared, and installed independently — similar to how a package manager handles dependencies.

**`.docgraph` project directory** — running `docgraph init` in any directory would create a `.docgraph/` folder (analogous to `.git/`) that holds the registry, ontology bundles, and cached Markdown extracts. The CLI would discover this folder automatically when invoked anywhere inside the project tree, so there would be no need to pass `--docgraph` explicitly. The output graph would also live inside `.docgraph/` by default, keeping the working directory clean.

## Dependencies

| Package | Role |
|---------|------|
| `anthropic` | Claude API client |
| `rdflib` | RDF graph, SPARQL, Turtle serialisation |
| `pyshacl` | SHACL validation |
| `click` | CLI |
| `rich` | Terminal output |
