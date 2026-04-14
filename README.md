# docgraph

Classify PDF documents and extract structured RDF data using Claude.

The project is configured through a small ontology registry (`docgraph.ttl`) that declares what ontologies to load, which OWL class is the classification target, which Claude model to use, and where to write results. Everything else — document categories, SHACL shapes, entity vocabularies — lives in plain Turtle files.

## How it works

1. **Load registry** — `.docgraph/docgraph.ttl` is discovered automatically and parsed. It lists local ontology files to merge and optional remote ontologies (FOAF, SKOS, PROV-O) to fetch.
2. **Convert PDF to Markdown** — each PDF is rendered to Markdown via Claude Vision and cached in `.docgraph/cache/`.
3. **Agent loop** — a Claude agent classifies the document, deduplicates entities against previously extracted results (via SPARQL), and extracts structured properties as JSON-LD.
4. **Persist** — results are appended to `.docgraph/results.ttl` as RDF triples in the configured output namespace.
5. **Validate** — the output graph is checked against SHACL shapes.

## Setup

```
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

This registers a `docgraph` command in your environment.

## Usage

### Initialise a project

```
docgraph init [directory]
```

Creates a `.docgraph/` folder in `directory` (default: current working directory).  Analogous to `git init` — run this once at the root of any project that will process documents.

```
my-project/
  .docgraph/
    docgraph.ttl              # project registry — edit this
    ontologies/
      financial_documents.ttl
      models.ttl
      shapes.ttl
    cache/                    # Markdown extracts cached here
    results.ttl               # output graph written here
```

### Classify documents

```
docgraph run <input.pdf|directory/>
```

The CLI walks up the directory tree from the input path to find `.docgraph/docgraph.ttl` automatically — no `--docgraph` flag needed when working inside an initialised project.  Results and Markdown caches are written inside `.docgraph/` by default, keeping the working directory clean.

Options for `run`:

| Flag | Default | Description |
|------|---------|-------------|
| `--docgraph` | auto-discovered | Override the registry path explicitly |
| `--min-confidence` | `0.5` | Skip hits below this threshold |
| `--force` / `-f` | off | Re-classify already-processed files |
| `--dry-run` / `-n` | off | Preview without writing results |
| `--offline` | off | Skip fetching remote ontologies listed in docgraph.ttl |
| `--note` | — | Free-text hint passed to the classifier |
| `--debug` | off | Print full prompts and LLM responses |

Results are written to the path declared in `docgraph:results` (`.docgraph/results.ttl` for initialised projects, `classified/results.ttl` for the legacy layout).

## Project layout

After `docgraph init`:

```
.docgraph/
  docgraph.ttl             # project registry — start here
  ontologies/
    financial_documents.ttl  # OWL classes for fin: document types
    shapes.ttl               # SHACL validation rules
    models.ttl               # LLM model declarations
  cache/                   # per-PDF Markdown extracts (gitignore this)
  results.ttl              # extracted RDF output (gitignore this)
```

Source layout:

```
src/
  agent.py               # Claude agent: classify → deduplicate → extract
  ontology.py            # registry loader, JSONLD_CONTEXT, namespace utils
  classifier.py          # PDF → Markdown content block
  validator.py           # SHACL validation wrapper
  results.py             # append/query results.ttl
  markdown_io.py         # Markdown cache read/write
  project.py             # .docgraph/ discovery and init
  tool/                  # Claude tool implementations
main.py                  # CLI entry point (commands: init, run)
```

The `data/` directory at the repo root contains the same ontology files used as defaults before `init` was introduced.  It is still loaded as a fallback when no `.docgraph/` project is found.

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

**Ontology bundles and standard ontology profiles** — instead of listing individual ontology files in `docgraph.ttl`, a bundle would package a coherent set of classes, shapes, and namespace declarations together under a single identifier. Bundles could be versioned, shared, and installed independently — similar to how a package manager handles dependencies.

Bundles also enable using standard ontologies (schema.org, P2P-O, DR-O) without sending their full content to the LLM. A bundle is a profile: it points at the standard ontology for URIs and semantic structure, then adds its own annotation overlay with LLM-facing metadata (`skos:notation`, `skos:definition`) and SHACL extraction shapes. The standard ontology stays untouched.

Since standard ontologies use different properties for descriptions (`rdfs:comment` in schema.org, `skos:definition` elsewhere), the bundle would declare a configurable priority list for resolving the class description and label used in the classification prompt:

```turtle
ex:MyBundle a docgraph:Bundle ;
    docgraph:descriptionProperties ( skos:definition skos:scopeNote rdfs:comment ) ;
    docgraph:labelProperties       ( skos:prefLabel rdfs:label ) .
```

`skos:altLabel` (synonyms: "Bill", "Faktura", "Sales Invoice") would be included in the classification prompt as "also known as" hints, helping the LLM recognise terminology variants. `skos:hiddenLabel` (misspellings, abbreviations, locale-specific terms) would be passed as a separate silent-matching hint — present for recognition, not shown as canonical names.

**Two-level classification** — as the number of document classes grows across domains (Finance, Medical, Legal, HR), sending the full class list to the LLM becomes impractical. Bundles map naturally to domains: the first classification pass picks the relevant bundle from a small stable list of domains; the second pass classifies within that bundle's classes. The LLM sees only the relevant subset at each step.

**Context dimensions** — bundles can declare context dimensions relevant to their domain. The classification pipeline detects or infers those dimensions from the input and uses them to filter labels and select shape variants. What those dimensions are is entirely up to the bundle — language and jurisdiction for financial documents, geographic region or taxonomic family for natural history, time period for archival records, and so on.

Language is a general-purpose dimension with built-in SKOS support: `skos:altLabel` and `skos:hiddenLabel` carry RDF language tags, so the classification prompt automatically shows only the labels relevant to the detected language. Other dimensions are domain-specific and declared by the bundle. A dimension can affect the classification prompt (filter which labels and definitions are shown), the extraction shapes (add required properties or format constraints), or both — and multiple dimensions can combine independently.

## Dependencies

| Package | Role |
|---------|------|
| `anthropic` | Claude API client |
| `rdflib` | RDF graph, SPARQL, Turtle serialisation |
| `pyshacl` | SHACL validation |
| `click` | CLI |
| `rich` | Terminal output |
