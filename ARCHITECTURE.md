# DocGraph — Architecture Design Notes

> Session date: 2026-04-15. Last updated: **2026-05-06** (groomed: dropped the dead Phase 1–4 analyzer pipeline that the 14-prompt classifier in `src/classify_part2/` replaced; trimmed cascade-delete and SHACL-derivation deep dives; deduped structural-class declarations; pruned open questions; connected the 14-prompt classifier to the template model; split foundational reference material into `docs/architecture/{meta-ontology,information-objects,provenance}.md`; dropped the verbose `tpl:Invocation` sub-template syntax and demoted it to an open question; cleaned `dg:noPart2Anchor` and `cache/{lifts,anchors}/` vestiges of dead Phase pipeline). Read this file at the start of any session continuing this design.

## Vision

DocGraph started as a financial-document extractor with a hardcoded ontology
(`financial_documents.ttl`). The goal is to be **fully general**: ISO 15926
Part 2 is the meta-ontology so the system can shift across domains without
hardcoding any one of them.

- **`docgraph init`** seeds only the meta-ontology — no domain classes.
- **`docgraph add <file>`** — the LLM figures out what kind of document it is
  and builds the knowledge graph accordingly.
- **`docgraph remove <file>`** — drops the source's named graph; references to
  its concepts are repaired or marked unresolved.

The original example (German invoice + EU standard defining Invoice + meta-
document classifying types of standards) is one of many. **Current focus**:
classification (Q1/Q2), template-instance recognition, and template discovery.

## Companion docs

Foundational reference material lives in `docs/architecture/`:

- [`meta-ontology.md`](docs/architecture/meta-ontology.md) — ISO 15926 Part 2
  as the meta-ontology. Why Part 2, when to reify vs use plain RDFS, the
  top-level hierarchy, the `dg:` extension namespace, the structural classes
  (`dg:Document` / `dg:Chapter` / `dg:Quote` / `dg:File`), modality
  individuals.
- [`information-objects.md`](docs/architecture/information-objects.md) — the
  file → document → chapter → quote chain, concrete turtle, design rules,
  PDF→Markdown derivation with PROV-O.
- [`provenance.md`](docs/architecture/provenance.md) — named-graph + Part 2
  reification provenance model, bundled `dg.ttl` + per-project `config.ttl`
  backbone, document-sourced assertions, cascade-delete, TTL ingest, DEFINE
  vs REFERENCE ownership and unresolved-concept stubs.
- [`templates.md`](docs/architecture/templates.md) — the Part 7-style
  lifted/lowered template system; recognition / expansion / SPARQL
  translation; library / structural / learned discovery.

This file (ARCHITECTURE.md) holds the active design surface: declares-axis,
modality, templates pointer + 14-prompt connection, storage layout,
classification, extraction pipeline, init, open questions.

---

## Pipelines — Part 2 and Part 14 in parallel

DocGraph supports two upper-ontology *pipelines* selected at `init` time:

| Pipeline | Upper ontology | Classifier path | Templates | Status |
|---|---|---|---|---|
| **`dg:Part2Pipeline`** | ISO 15926 Part 2 RDF (POSC Caesar) — full reified Part 2 | `src/classify_part2/` | `data/templates/iso/` | Current default. **Frozen** — no new feature work; bug fixes only. |
| **`dg:Part14Pipeline`** | ISO 15926 Part 14 OWL (`vendor/ontologies/LIS-14.ttl`) — DL rendering of Part 2 | `src/classify_part14/` | `data/templates/iso14/` | New, **template-driven from day 1**. Built in parallel; default once at parity. |

Why two: Part 2 carries the full reified-relationship model (`Classification`,
`Description`, `Composition`, …) needed for sourced/temporal/authority-bound
content. Part 14 is the OWL DL "lifted" rendering of the same model — direct
properties (`lis:hasPart`, `lis:representedBy`, …) instead of reified clusters
— which aligns with what modern Reference Data Libraries publish (POSC Caesar,
IOGP, CFIHOS, 15926.io). Migrating loses Part 2's per-assertion expressiveness
in exchange for RDL federation, OWL-tooling support, and a much smaller
on-disk footprint per template instance. Provenance content that previously
lived in reified clusters moves to PROV-O (`prov:wasDerivedFrom`,
`prov:generatedAtTime`, `prov:wasAttributedTo`) plus named-graph context.

A project commits to one pipeline at `init` time — mixing produces incoherent
named graphs (Part 2 reified clusters next to Part 14 direct properties). The
choice is recorded in `.docgraph/config.ttl`'s `dg:pipeline` triple; the CLI
dispatcher reads it and routes to the matching classifier package.

### Part 14 build-out — milestones

The Part 14 pipeline is built incrementally without modifying `classify_part2`.
Default flips from `part2` to `part14` at M3:

- **M0 — loader & init refactor**: `docgraph init --pipeline part14` produces
  `.docgraph/config.ttl` (no `meta.ttl` copying); loader reads
  `vendor/ontologies/{LIS-14.ttl, dg.ttl, tpl.ttl, prov-o.ttl}` based on the
  pipeline triple. Parity with current Part 2 init flow.
- **M1 — structural-only ingest**: `docgraph add file.pdf` against a Part 14
  project produces a valid Part 14 named graph with file/document/chapter/
  quote chain (`lis:representedBy` + `lis:hasPart`) plus Q1 subject
  classification. **Skips** 14-aspect extraction.
- **M2 — partial aspect coverage**: template-driven extraction for 5 of the
  14 aspects (start with the cheapest: identifiers, classes, properties,
  individuals, classifications). Templates land under `data/templates/iso14/`.
- **M3 — full aspect coverage at parity**: all 14 aspects covered on the
  existing test corpus with parity-or-better quality. **`docgraph init`
  default flips to `part14`** here.
- **M4 — `classify_part2` retired**: code removed; `Part2Pipeline` value kept
  in `dg.ttl` for legacy projects that haven't migrated their data.

### Open question — possible vs actual individuals

Part 14 commonly uses **named graphs themselves** to distinguish actual
individuals from possible individuals (e.g., a graph holding RDL classifiers
declares "every individual herein is a possible individual"). Our template
declaration model works at the per-instance level — `var:x rdf:type ex:Foo`
— and can't naturally express graph-level classifications of this kind.
Out-of-scope for the initial Part 14 build-out; will need a graph-level
template shape or an explicit graph-classification mechanism later.

---

## What does a document declare?

Independent of *what* a document is about, every source declares some
combination of classes, properties, and individuals. The ingester records this
as `<source> dg:defines …` triples:

| Question | Stored as | Triggered by |
|---|---|---|
| Defines classes? | `<source> dg:defines dg:Classes` | `?x a owl:Class`, `rdfs:Class`, `skos:Concept`, … |
| Defines properties? | `<source> dg:defines dg:Properties` | `?x a owl:ObjectProperty`, `owl:DatatypeProperty`, `rdf:Property`, … |
| Defines individuals? | `<source> dg:defines dg:Individuals` | `?x a <some-class-not-in-the-meta-vocabulary>` |

Any combination is valid. An ontology TTL with named individuals → all three.
A receipt PDF → `dg:Individuals` only. A standards PDF defining what an
Invoice is → `dg:Classes` and `dg:Properties` (and possibly some illustrative
individuals).

This **declares-axis** is orthogonal to the **subject (Q1)** and **form (Q2)**
classification — see "Classification" below. A document that *defines*
`schema:Invoice` is not the same as a document that *is* an instance of
`schema:Invoice`; Q1/Q2 answer the latter, the declares-axis answers the
former. Both can apply to the same source.

---

## Modality

Modality is extracted directly from normative text and stored as a triple on the
**template declaration** that defines the predicate (see "Templates" below — every
domain predicate is the lifted form of a template). The template's TTL file lives
in `data/templates/<domain>/` and carries `tpl:modality` alongside its other
metadata:

```turtle
# data/templates/financial/invoice-has-vat-number.ttl
# Template extracted from "The Seller VAT identifier MUST be present"
@prefix var: <urn:tpl-var/> .

dom:InvoiceHasVatNumber a tpl:Template ;
    rdfs:label    "VAT Number on an invoice" ;
    tpl:definition "[invoice] has VAT identifier [value]." ;
    tpl:slot     var:invoice, var:value ;
    tpl:modality dg:Mandatory ;                               # MUST
    tpl:lowered  var:lowered .

var:invoice tpl:range dom:Invoice .
var:value   tpl:range xsd:string .

GRAPH var:lowered {
    var:invoice dom:hasVatNumber var:value .
}

# "The buyer reference SHOULD be provided"
dom:InvoiceHasBuyerRef a tpl:Template ;
    rdfs:label   "Buyer reference on an invoice" ;
    tpl:slot     var:invoice, var:value ;
    tpl:modality dg:Preferred ;                               # SHOULD
    tpl:lowered  var:lowered .

var:invoice tpl:range dom:Invoice .
var:value   tpl:range xsd:string .

GRAPH var:lowered {
    var:invoice dom:hasBuyerReference var:value .
}
```

Modality is a `dg:`-namespace simplification, not a reified Part 2 chain — modality
is a structural property of the *template definition*, not an event-with-extent,
so plain `tpl:modality` is the right shape.

---

## Templates — Part 7-style lifted/lowered patterns

Templates are the **universal LLM-emit and storage mechanism**: every
LLM-emitted assertion is a template instance, every domain ontology is a
template library, and **the on-disk graph is the lifted form** — compact
typed-anchor + slot-binding triples. The lowered Part 2 cluster is
materialised on demand for SPARQL paths that need Part 2 shapes; it isn't
stored. Templates are grounded to Part 2 through their lowered body, so
materialisation is always possible without losing data.

The full chapter — lifted/lowered semantics, the `var:` namespace and
skolemization, instance-form and pattern-form examples, the reification
spectrum, multi-valued slots, sub-template composition, deterministic URI
minting, recognition via on-the-fly SPARQL translation, the LLM emit format,
storage layout, domain libraries as template directories, the three-source
discovery model (library / structural / learned), and cascade behaviors —
lives in **[`docs/architecture/templates.md`](docs/architecture/templates.md)**.

### 14-prompt classifier as an in-progress template library

The existing pipeline at `src/classify_part2/` runs **14 prompts** (one per
ISO 15926 Part 2 aspect: activities, classes, connections, identifiers,
individuals, lifecycle, participations, properties, roles, temporal,
whole-parts, …). Each prompt's converter
(`src/classify_part2/convert/<aspect>.py`) takes the LLM's JSON output and
emits a reified Part 2 cluster.

Mapped onto the template model: **each converter's output is the lowered body
of a corresponding template** in the library. The 14 prompts are doing
template expansion by hand today; the migration is mechanical — each
converter becomes a template definition under `data/templates/iso/`, and the
generic expander (`src/templates/expand.py`) replaces the per-converter
Python.

Until that migration lands, the 14-prompt pipeline keeps its current shape;
the template engine (`src/templates/`) is developed in parallel against
synthetic and user-supplied templates.

---

## Storage layout — installation / project / results

Three layers, each with a different category of state and a different lifetime:

| Layer | Owns | Lives in | Versioned with |
|---|---|---|---|
| **Bundled foundationals** | Operational ontologies docgraph itself depends on (LIS-14, ISO-15926-2 RDF, PROV-O, base `dg:`/`tpl:` declarations) | `vendor/ontologies/` (per docgraph install) | docgraph release |
| **Project assets** | User-authored vocabulary and templates (custom domain ontologies, hand-written templates) | the project repo's `data/` (e.g. `data/templates/<custom>/`, `data/ontologies/<custom>.ttl`) | project's git |
| **Results + caches** | Per-project state generated by docgraph (per-source graphs, registries, caches, RDL mirrors, unresolved stubs) | `.docgraph/` | not versioned (gitignored) |

Bundled foundationals are *never* copied into `.docgraph/`; the loader reads them from `vendor/ontologies/` on startup. Project assets are also never copied — the loader reads them from the project repo by path. `.docgraph/` is exclusively the regenerable per-project state, and deleting it leaves the project's input documents and assets untouched.

### `.docgraph/` directory

**One source document → one TTL file.** Each source gets its own named-graph TTL file under `graphs/` so the result is easy to inspect by eye. A registry tracks all sources.

```
.docgraph/
  config.ttl             ← project metadata: pipeline choice, init date, version
  sources.ttl            ← registry: source path → graph file → added date, declares-axis
  templates.ttl          ← registry of loaded user-authored templates (paths only — TTL files live in the project repo)
  graphs/
    _unresolved.ttl      ← stubs for references not yet resolved
    <slug>.ttl           ← one per ingested source (named graph)
  cache/
    pdfmd/               ← PDF → Markdown cache (per-document, key = doc hash)
    templates/           ← LLM-discovered templates pending user approval
  rdl/                   ← local mirrors of external Reference Data Libraries
    <name>/
      mirror.ttl         ← the dump
      bm25-index/        ← lexical index for resolution
      metadata.ttl       ← endpoint URL, version, last-refresh date
```

`.docgraph/` is regenerable from the original sources. Every file in it is either a result of ingestion (`graphs/`, `sources.ttl`, `_unresolved.ttl`) or a cache/mirror (`cache/`, `rdl/`). Deleting `.docgraph/` and re-running `docgraph add` for each registered source rebuilds the project state.

The `lis:` or `iso15926:` (depending on pipeline) and `dg:` prefixes are pre-bound in every graph file for readability.

### `config.ttl`

The per-project header is intentionally tiny — no `owl:imports` chain to maintain, no copies of upstream files to keep in sync:

```turtle
@prefix dg:  <http://example.org/docgraph/meta#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<> a dg:DocgraphProject ;
    dg:pipeline   dg:Part14Pipeline ;
    dg:createdAt  "2026-05-09"^^xsd:date ;
    dg:version    "0.x" .
```

`dg:pipeline` is one of `dg:Part2Pipeline | dg:Part14Pipeline` and decides which classifier path runs (`src/classify_part2/` vs `src/classify_part14/`) and which bundled ontology set is loaded. A project commits to one pipeline at `init` time; mixing pipelines would produce incoherent named graphs (Part 2 reified clusters next to Part 14 direct OWL properties). Switching an existing project to a different pipeline requires re-ingesting sources.

### Loader recipe

The CLI's loader runs once per command, building an in-memory rdflib `Dataset` with every graph relevant to the project:

1. Read `.docgraph/config.ttl`. Determine the pipeline.
2. Load the bundled foundational set for that pipeline:
   - **Part 14**: `vendor/ontologies/LIS-14.ttl` + `dg.ttl` + `tpl.ttl` + `prov-o.ttl`.
   - **Part 2**: `vendor/ontologies/ISO-15926-2_2003.rdf` + `dg.ttl` + `tpl.ttl` + `prov-o.ttl`.
3. Load the bundled template library for that pipeline (`data/templates/iso14/` or `data/templates/iso/`, plus any enabled bridges under `data/templates/bridges/`).
4. Load any user-authored templates referenced from `.docgraph/templates.ttl`.
5. Iterate `.docgraph/sources.ttl` and load each per-source graph from `.docgraph/graphs/<slug>.ttl` into its named graph.
6. When relevant (resolution queries during ingest), attach RDL mirrors from `.docgraph/rdl/<name>/`.

The standard OWL `owl:imports` mechanism is **not** used to drive resolution — there's no reasoner walking import chains, no IRI-to-file catalog. The loader is a deterministic file-reader following the recipe above; "imports" are encoded as code, not as triples on disk.

### Graph files are real files

Regardless of input format, `graphs/<slug>.ttl` is a real file written by the
ingest — never a symlink to the source. The output is a *normalized view*
(canonical triples + Part 2-anchored classes + reified clusters from the
14-prompt classifier) that is rarely byte-identical to the source. Storing it
as a real file lets cascade-delete drop it cleanly without touching the
user's original input.

The original TTL/PDF source stays where the user put it; the registry
references it by path, but the graph is ours.

### sources.ttl example

```turtle
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix dg:       <http://example.org/docgraph/meta#> .

<source/eu-standard.pdf>  a dg:IngestionRecord ;
    dg:sourcePath "eu-standard.pdf" ;
    dg:graphFile  ".docgraph/graphs/eu-standard.ttl" ;
    dg:addedAt    "2026-04-15"^^xsd:date ;
    dg:defines    dg:Classes, dg:Properties .       # standards doc — defines vocabulary

<source/german-invoice.pdf>  a dg:IngestionRecord ;
    dg:sourcePath "german-invoice.pdf" ;
    dg:graphFile  ".docgraph/graphs/german-invoice.ttl" ;
    dg:addedAt    "2026-04-15"^^xsd:date ;
    dg:defines    dg:Individuals .                  # instance document

<source/schemaorg.ttl>    a dg:IngestionRecord ;
    dg:sourcePath "schemaorg-current-https.ttl" ;
    dg:graphFile  ".docgraph/graphs/schemaorg.ttl" ;
    dg:addedAt    "2026-04-27"^^xsd:date ;
    dg:defines    dg:Classes, dg:Properties, dg:Individuals .  # full vocab
```

`dg:IngestionRecord`, `dg:sourcePath`, `dg:graphFile`, `dg:addedAt`, `dg:defines`,
`dg:Classes`, `dg:Properties`, `dg:Individuals` are docgraph-specific.

## Classification — two questions (Q1 + Q2)

Classification of an ingested document splits into two independent questions asked in
order. They have different scopes, different candidate sets, and different cost
profiles.

These are orthogonal to the **declares-axis** above (*what does this document
define?* — Classes / Properties / Individuals). Q1/Q2 ask about the
document's subject and form. The declares-axis is structural inspection of
triples; Q1/Q2 are LLM-driven semantic calls. Both result sets land on the
same `<source>` IngestionRecord but answer different questions.

### Q1 — Subject: what is this document *about*?

- **Stored as**: `<source> dg:isAbout <UpperClass>, …` (zero or more values).
- **Candidate scope**: a curated **upper-level Part 2 class set** (~15 classes:
  `ArrangedIndividual`, `PhysicalObject`, `Organism`, `Person`, `Organization`,
  `Event`, `Activity`, `Role`, `Quality`, `Disposition`, `Function`, etc.).
  - Full Part 2 has 100+ classes — too many for a single LLM call. We don't send the
    whole catalogue; we send a curated upper-level subset that's stable across
    ingests.
  - PROV-O is intentionally excluded — we use it for *metadata/provenance*
    (`prov:Activity`, `prov:wasGeneratedBy`, …), not as a subject vocabulary.
    Including it would conflate "what the document is about" with "what happened
    during ingest."
  - DCMI Terms is also excluded — its classes overlap with Part 2 and introduce noise.
- **Set size**: ~15 curated classes. Cheap enough to send the whole list to the LLM
  with no embedding pre-filter. RAG is not used here.
- **Always runs**, regardless of whether a domain ontology is loaded. This is the
  question that's *always* answerable: every document is at least intuitively "about"
  something at the upper-ontology level.
- **Examples**:
  - Zahnrechnung (dental invoice) → `dg:isAbout iso15926:Activity, iso15926:Person`
    (the dental procedure, the participants).
  - PROV-O ontology document → `dg:isAbout iso15926:Activity,
    iso15926:ArrangedIndividual` (it defines activity/entity vocabulary).
  - Sensor reading → `dg:isAbout iso15926:Quality`.
  - Poetry book → `dg:isAbout iso15926:ArrangedIndividual` (vague — and that
    vagueness is itself the "outside our domain" signal).
- **Doubles as the uncovered diagnostic**: if Q1 returns only the most
  generic subjects (`ArrangedIndividual` and nothing more specific) with low
  confidence, the document is outside the upper ontology's resolution.

### Q2 — Form: what *kind of document* is this?

- **Stored as**: `<source> rdf:type <FormClass>` (single value).
- **Candidate scope**: leaf classes from **user-ingested ontologies only**.
  - "User-ingested" = declared in a named graph that came from
    `docgraph add <file>.ttl`. Bundled foundationals (Part 2, PROV-O, DCMI, docgraph
    meta) don't contribute form candidates — they're scaffolding, not subject matter.
    (If a user ingests Part 2 a second time deliberately, it joins the candidate pool
    — opting in is allowed.)
  - "Leaf" = no other class declares this as its `rdfs:subClassOf` parent in the
    combined dataset. Abstract intermediates like `fin:FinancialDocument` (which has
    4 subclasses) are filtered out — the LLM should always pick the most specific
    class.
  - The leaf rule is structural; no per-class annotation is needed.
- **Set size**: variable. Small (5 in the toy financial example), large in real domain
  ontologies (200+ in a procurement RDL).
- **RAG as a count-based optimization**: when there are ≥ 30 candidates, the embedding
  store narrows to top-30 by cosine similarity before the LLM call; otherwise the
  candidate list is sent intact. Below 30 the prompt is cheap enough that filtering
  loses information without saving meaningfully.
- **Conditionally runs**: when no user ontology is loaded, Q2 is skipped with a clear
  message ("no domain ontology — `docgraph add <ontology.ttl>` first"), not an opaque
  "uncovered" gate.

### Why the form-vs-subject distinction matters

A common ontology-design mistake is to flatten form and event into the same class
hierarchy. The financial ontology in `data/financial_documents.ttl` correctly keeps
them separate — and is the model for how domain ontologies should be authored:

```turtle
# Form branch — documents (subClassOf iso15926:ArrangedIndividual)
fin:FinancialDocument     rdfs:subClassOf iso15926:ArrangedIndividual .
fin:DemandForPayment      rdfs:subClassOf fin:FinancialDocument .
fin:ConfirmationOfPayment rdfs:subClassOf fin:FinancialDocument .
fin:Quote                 rdfs:subClassOf fin:FinancialDocument .
fin:Statement             rdfs:subClassOf fin:FinancialDocument .

# Event branch — financial activities (subClassOf prov:Activity ⊑ iso15926:Activity)
fin:Transaction  rdfs:subClassOf prov:Activity .
fin:Payment      rdfs:subClassOf fin:Transaction .
fin:Transfer     rdfs:subClassOf fin:Transaction .
fin:Payout       rdfs:subClassOf fin:Transaction .
```

A specific Zahnrechnung answers both questions from the right branches:
- Q1 (subject) → `dg:isAbout iso15926:Activity` — the underlying payment/treatment.
- Q2 (form)   → `rdf:type fin:DemandForPayment` — the layout/document kind.

If a domain ontology mixes the two — e.g., declares "Invoice" as both a form and an
event under one class — both questions return the same answer and the distinction
collapses. That's a *modelling* failure, not a pipeline failure.

### Q1 narrowing Q2 (deferred)

The natural follow-up question is whether Q1's answer can pre-filter Q2's candidate
set ("the document is about an Activity → consider only form classes structurally
related to Activity"). This is a real optimization for projects with 100+ form classes,
but requires a relevance-mapping mechanism between forms and subjects. Three honest
options when the time comes:

- Embedding affinity between form and subject `class_text`s.
- Property analysis: a form is relevant to a subject if any of its declared
  `rdfs:range`s reference the subject (or a transitive subclass).
- LLM-judged once at ontology-add: "for each form class, what upper-ontology subject is
  it most concerned with?" Tag as `dg:concernsSubject`.

For current scales (small handcrafted ontologies), independent Q1 + Q2 is sufficient.
The cascade is future work; the embedding store is already in place to power option 1
when needed.

### Coverage signals

Per ingest, the default graph carries:

```turtle
<ext/<slug>>
    dg:subjectConfidence  0.81 ;            # Q1's headline confidence
    dg:typeConfidence     0.92 ;            # Q2's headline confidence (if Q2 ran)
    dg:isAbout            iso15926:Activity, iso15926:Person .  # Q1 result
```

Reading them together: high `subjectConfidence` + Q2 didn't run → "we know
what it's about; you haven't loaded a form ontology yet". High
`subjectConfidence` + low `typeConfidence` → "we know the general topic; no
loaded form fits — the document is outside this project's domain coverage".

---

## Extraction pipeline (full sequence)

```
docgraph add <file>
    │
    ├─ 0. Validate, hash for idempotency, check existing entry.
    │
    ├─ 1. Register file as iso15926:ArrangedIndividual + prov:Entity
    │     (file metadata: hash, size, mime, pdfinfo: pages, title, ...).
    │     Mint the document ArrangedIndividual + reified RepresentationOfThing
    │     linking file → document (per the information-objects chain above).
    │
    ├─ 2. Format-specific extraction (front half).
    │     ├─ [.ttl / .n3]  Parse → candidate triples (the source's own vocab).
    │     └─ [.pdf]        PDF → Markdown via Claude vision (cached) →
    │                      LLM extracts candidate triples from the Markdown.
    │                      Both PDF→MD and the extract are recorded as
    │                      prov:Activity in the default graph.
    │                      Mint chapter/quote ArrangedIndividuals + composition
    │                      tuples while walking the markdown structure.
    │
    ├─ 3. Structural inspection — what does this source declare?
    │     Emit <source> dg:defines dg:Classes/Properties/Individuals
    │     (see "What does a document declare?" above).
    │
    ├─ 4. 14-prompt Part 2 classifier (src/classify_part2/).
    │     Run the per-aspect prompts (activities, classes, connections,
    │     identifiers, individuals, lifecycle, participations, properties,
    │     roles, temporal, whole-parts, …). Each converter emits a reified
    │     Part 2 cluster — equivalent to expanding the lowered body of the
    │     corresponding library template (see "Templates" above).
    │
    ├─ 5. Q1 — Subject identification (LLM, semantic).
    │     Candidates: ~15 curated upper-level Part 2 classes, sent in full.
    │     Emit <source> dg:isAbout <UpperClass>, …  Always runs.
    │
    ├─ 6. Q2 — Form classification (LLM, semantic; only when domain ontology loaded).
    │     Candidates: leaves of user-ingested ontologies.
    │     If ≥ 30: embedding top-k pre-filter; else send all.
    │     Emit <source> rdf:type <FormClass> in the extraction graph.
    │     Skipped (with clear message) when no domain ontology is loaded.
    │
    ├─ 7. Template-instance recognition + filling (in progress).
    │     Fold extracted facts against the loaded template library by
    │     recognition (see templates.md). The un-folded remainder feeds
    │     the discovery mechanisms (structural / learned).
    │
    └─ 8. Emit named graph and register in sources.ttl.
```

The extraction graph is described as a `prov:Entity` in the default graph,
generated by the LLM activities above. See
[`docs/architecture/provenance.md`](docs/architecture/provenance.md) for the
cascade story.

---

## What `docgraph init` produces

After init, `.docgraph/` contains only:

```
config.ttl              ← project metadata (pipeline, init date, version)
sources.ttl             ← empty registry
templates.ttl           ← empty user-template registry
graphs/_unresolved.ttl  ← empty stub
cache/pdfmd/            ← empty
cache/templates/        ← empty
```

No copies of foundational ontologies. No domain classes. No `owl:imports` chain to maintain. Bundled foundationals stay in `vendor/ontologies/`; the loader reads them from there based on the pipeline declared in `config.ttl`. Bundled `dg.ttl` and `tpl.ttl` carry the docgraph and templating extension vocabularies (structural classes like `dg:Document`/`dg:Quote`/`dg:File`, modality individuals, the `tpl:Template`/`tpl:slot`/`tpl:lifted`/`tpl:lowered` machinery, etc.) — see [`docs/architecture/meta-ontology.md`](docs/architecture/meta-ontology.md) for the full inventory.

`docgraph init --pipeline part2 | part14` selects the pipeline. Default is `part2` until `classify_part14` reaches feature parity (M3 in the parallel-pipelines plan); the default flips to `part14` then. Switching an existing project to a different pipeline requires re-ingesting sources, since per-source graphs are written in the chosen pipeline's idiom.

### Future: triplestore migration

Current plan uses **rdflib `Dataset`** with TriG/N-Quads format. The file
layout maps 1-to-1 to a triplestore's named graphs (Oxigraph, Apache Fuseki)
when scale demands it.

---

## Open questions / next decisions

1. **Merge conflicts**: Two documents declare the same URI as `owl:Class` with
   different `rdfs:subClassOf` parents. Options: last-write-wins, explicit
   conflict node (`dg:ConflictingDefinition`), or require user resolution.

2. **Templates: breadcrumb policy** *(closed 2026-05-07)*: Moot once storage
   is the lifted form — the typed anchor (`<inst-uri> a iso:Foo`) is itself
   the breadcrumb. The fold-back from lowered to lifted only matters for
   foreign Part 2 data ingested without going through the template-emit path,
   where `recognize` runs over the foreign cluster.

3. **Templates: sub-template composition syntax**: A template's lowered
   body should be able to invoke another template by name (so leaf
   templates are the only places raw Part 2 appears, everything else
   composes leaves). The earlier `tpl:Invocation / tpl:invokes / tpl:bind /
   tpl:role / tpl:value` proposal was rejected as too verbose. Likely
   replacement: embed a typed instance of the invoked template directly in
   the lowered body, with slot bindings as plain properties — but the
   exact shape (how slot URIs resolve, prefix conventions) is open.
   Whatever form wins must be recursively expanded at template-load time,
   with circular-invocation detection.

4. **Templates: versioning & replacement**: When a template definition
   changes (slot added, lowered body restructured) and there are existing
   expanded instances on disk, what's the migration story? Options: (a)
   re-expand all affected sources from cached LLM outputs (requires keeping
   LLM-emitted template-instance JSON, not just the expanded result); (b)
   leave existing data alone, new instances use new shape (graph drift);
   (c) require explicit `docgraph templates migrate <uri>` with diff preview.
   Probably (c) for explicit breaking changes, (b) for additive ones.

5. **Templates: foreign-Part-2 recognition at ingest**: When ingesting a
   TTL that already contains reified Part 2 clusters (not authored as
   templates), should ingest try to recognize known templates and re-author
   as instance-form, or leave the raw reified form? Recognition is cheap
   (subgraph match) and gives a cleaner result; but it changes the source's
   intent ("the source emitted X triples" becomes "the source emitted Y
   template instances"). Probably leave-raw by default, with
   `docgraph templates fold <source>` as an explicit pass.

6. **Subject classifier implementation**: The subject-typed filling step
   needs a fragment-to-Part-2-subject classifier. Options: (a) rule-based on
   extractor cues (table-row → likely Possession; verb-phrase → likely
   Activity); (b) lightweight LLM pass (cheap model, single classification
   call per fragment); (c) a recursive use of the template engine itself —
   pattern-form classifier templates whose lifted side is a natural-language
   descriptor and lowered side a `tpl:subject` annotation. Probably (a)+(b)
   hybrid: rules where they're obvious, LLM fallback otherwise.

7. **Pattern-index signature shape**: How deep should subgraph signatures go
   (2-walks vs 3-walks vs bounded-by-reification-cluster)? Type-only or
   predicate-aware? Promotion threshold `k`? Defaults: bounded by the
   enclosing reified cluster (e.g., one full `Description` tuple),
   predicate-aware, `k=3` across ≥2 sources. Tune once real data exists.
   Risk of the deeper-walk setting: signatures explode combinatorially.
   Risk of shallow: too many spurious matches.

8. **Structural-template extraction scope**: Which document features count
   as "structural repetition" worth lifting at state-0? Tables yes; numbered
   lists yes; key-value blocks (forms) yes; prose paragraphs no. Edge cases:
   nested tables, tables with merged cells, diagrams with consistent
   sub-structure (org charts, P&IDs). Probably tackle markdown tables first,
   then expand.
