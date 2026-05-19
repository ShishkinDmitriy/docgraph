# docgraph

Build an ISO 15926 Part 14 knowledge graph from PDF documents using an LLM.

The pipeline is a small task DAG. Each phase reads the doc graph state, does
its work, and writes an additive delta (TriG) under `.docgraph/docs/<slug>/`.
Re-runs are idempotent: every task has a dirty check, and only stale work runs.

## Setup

```
pip install -e .
export ANTHROPIC_API_KEY=sk-ant-...
```

This registers a `docgraph` command (also installed as `dg`).

## Usage

Everything is a task. The CLI dispatches by name:

```
docgraph <task> [args...]
docgraph -f <task> [args...]    # force the task even if its dirty check is clean
docgraph -d <task> [args...]    # verbose logging (LLM prompts + responses)
docgraph help <task>            # show the task module docstring
```

`docgraph tasks` prints the registered DAG as a tree with one-line
descriptions per task — the fastest way to discover what's available.

### Common commands

```
docgraph init [DIR]              # create .docgraph/ (default: cwd)
docgraph add FILE.pdf            # ingest a PDF (runs the full pipeline)
docgraph status                  # list ingested sources
docgraph snapshot TARGET [SEQ]   # write graph[.NNN].ttl from materialised deltas
docgraph diagram TARGET          # render diagram[.NNN].puml + .svg
docgraph view TARGET             # open annotated.html in browser
docgraph history TARGET          # list the delta history
docgraph diff TARGET SEQ_A SEQ_B # show triples added/removed between seqs
docgraph coverage TARGET         # report which HTML units the graph cites
docgraph consolidate             # promote ext: classes shared across ≥N docs
docgraph enrich TARGET           # refine entity types via external RDL
docgraph clean [DIR]             # wipe every ingested source from a project
```

`TARGET` is either a slug (registered in `sources.ttl`) or a path to the
original source file (resolved by content hash).

Individual pipeline phases (`recognize`, `convert`, `extract`, `templates`,
`align`, `register`, `snapshot`, `diagram`) can also be invoked directly:

```
docgraph convert FILE.pdf        # run only up to convert (and its deps)
docgraph -f extract FILE.pdf     # re-run extract even if its delta is current
```

## Task DAG

`docgraph add` is the composite root that pulls in everything. Each task's
deps are declared on its `@docgraph.task` decorator; `docgraph tasks` walks
them to print:

```
add — Ingest a PDF: run the full per-doc pipeline
└── diagram — Render PlantUML diagram from the doc snapshot
    └── snapshot — Materialize doc graph to graph[.NNN].ttl
        ├── identity — Resolve per-doc identifiers (slug, URIs, hashes)
        │   ├── resolve_project — Resolve the enclosing .docgraph/ project root
        │   ├── resolve_slug — Resolve target arg to a registered doc slug
        │   └── setup_llm — Configure the LLM client + model from env
        └── register — Write the source entry into sources.ttl
            └── align — Align doc-local ext classes to higher-scope canonicals
                └── templates — Fold lowered patterns into template invocations
                    └── extract — Extract entities + properties via mega-walker LLM
                        └── load_html — Load converted.html intermediates into ctx
                            └── convert — Convert PDF → HTML/Markdown via vision LLM
                                └── recognize — Recognize PDF: type + file-metadata
```

Plus several read-only meta tasks (`status`, `history`, `diff`, `view`,
`coverage`, `tasks`) and project-wide tasks (`clean`, `consolidate`).

## Project layout

```
.docgraph/
  config.ttl                       # tiny header (creation date, version)
  sources.ttl                      # registry of ingested sources
  templates.ttl                    # user-authored template registrations
  docs/<slug>/                     # one dir per ingested doc
    delta.NNN.trig                 # versioned per-step deltas
    graph.ttl                      # HEAD snapshot (written by `snapshot` task)
    graph.NNN.ttl                  # historical snapshots (`snapshot SLUG N`)
    diagram.{puml,svg,png}         # HEAD diagram
    diagram.NNN.{puml,svg,png}     # historical diagrams
    converted.html                 # canonical HTML view (vision LLM output)
    converted.md                   # markdown projection for LLM prompts
    annotated.html                 # entity-highlighted view (`view` task)
  cache/                           # PDF→Markdown intermediate cache
  embeddings.npz                   # ext-class embedding store (consolidate)
```

The deltas are the source of truth. `graph.ttl` is a materialised snapshot
that any downstream tool (the diagram renderer, the annotated viewer) reads
without re-walking deltas. Snapshots are regenerable any time.

## Source layout

```
main.py                            # CLI dispatcher (~90 lines)
src/
  tasks/                           # the task DAG
    framework.py                   # Registry, @task / @dirty decorators, runner
    _registry.py                   # the singleton `docgraph` registry
    <task>.py                      # one module per task
  extract_part14/                  # ISO 15926 Part 14 pipeline helpers
    loader.py                      # builds the in-memory Dataset
    structural.py                  # recognize + convert delta builders
    mega_walker.py                 # entity + property + ext-class extraction
    template_recognizer.py         # SPARQL template folding
    align.py                       # scope-walking class deprecation
    consolidate.py                 # cross-doc ext-class promotion
    enrich.py                      # external RDL refinement
  sources.py                       # sources.ttl I/O
  deltas.py                        # delta read/write, materialize()
  project.py                       # .docgraph/ layout helpers
  html_io.py                       # PDF → HTML/Markdown via vision LLM
  diagram.py                       # (gone — absorbed into tasks/diagram.py)
  llm/                             # Anthropic client
vendor/ontologies/                 # bundled foundationals (LIS-14, dg, prov, …)
data/templates/                    # bundled template definitions
```

`ARCHITECTURE.md` and `docs/architecture/` carry the design notes — read those
when extending the task DAG or the Part 14 pipeline.

## Dependencies

| Package    | Role                                           |
|------------|------------------------------------------------|
| `anthropic` | LLM client (vision + extraction)              |
| `rdflib`   | RDF graph, SPARQL, Turtle/TriG serialisation   |
| `click`    | CLI                                            |
| `rich`     | Terminal output (tables, trees, progress logs) |
