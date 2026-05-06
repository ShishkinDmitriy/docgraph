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
  reification provenance model, `meta.ttl` permanent backbone, document-
  sourced assertions, cascade-delete, TTL ingest, DEFINE vs REFERENCE
  ownership and unresolved-concept stubs.
- [`templates.md`](docs/architecture/templates.md) — the Part 7-style
  lifted/lowered template system; recognition / expansion / SPARQL
  translation; library / structural / learned discovery.

This file (ARCHITECTURE.md) holds the active design surface: declares-axis,
modality, templates pointer + 14-prompt connection, storage layout,
classification, extraction pipeline, init, open questions.

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

Templates are the **universal LLM-emit and storage-grounding mechanism**: every
LLM-emitted assertion is a template instance, every domain ontology is a
template library, every Part 2 reified cluster on disk is the lowered form of a
template. Storage stays uniformly Part 2-shaped because each template's lowered
body is grounded to Part 2.

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

## Storage layout (file-based, no triplestore yet)

**One source document → one TTL file.** Each source gets its own named-graph TTL file
under `graphs/` so the result is easy to inspect by eye. A registry tracks all sources.

```
.docgraph/
  meta.ttl             ← imports Part 2 + dg: extensions (written by `init`, never overwritten)
  sources.ttl          ← registry: source path → graph file → added date, detected role
  graphs/
    _unresolved.ttl    ← stubs for concepts referenced before they were defined
    <slug>.ttl         ← one file per source document (named graph)
  cache/               ← existing PDF-to-markdown cache (unchanged)
```

The `iso15926:` and `dg:` prefixes are pre-bound in every graph file for readability.

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
meta.ttl       ← imports ISO 15926 Part 2 + declares dg: and tpl: extensions
                 (dg:Document, dg:Chapter, dg:Quote, dg:File, dg:PdfFile, dg:MarkdownFile,
                  dg:Modality, dg:Mandatory/Preferred/Optional/Prohibited, dg:modality,
                  dg:status, dg:Unresolved, dg:IngestionRecord,
                  dg:defines, dg:Classes/Properties/Individuals,
                  dg:text, dg:locator,
                  tpl:Template, tpl:Slot, tpl:slot, tpl:range, tpl:minCount,
                  tpl:maxCount, tpl:lifted, tpl:lowered, tpl:subject,
                  tpl:definition)
sources.ttl    ← empty registry
templates.ttl  ← empty template registry (which template files are loaded)
graphs/        ← contains only an empty _unresolved.ttl
cache/
  pdfmd/       ← PDF → Markdown cache (per-document, key = doc hash)
  templates/   ← LLM-discovered templates, user-approved (per-template URI)
```

No `financial_documents.ttl`. No domain classes. The graph is empty except for
structure. When the combined graph is loaded, `meta.ttl`'s `owl:imports` brings in
Part 2 and the full hierarchy is available for classification.

### Future: triplestore migration

Current plan uses **rdflib `Dataset`** with TriG/N-Quads format. The file
layout maps 1-to-1 to a triplestore's named graphs (Oxigraph, Apache Fuseki)
when scale demands it.

---

## Open questions / next decisions

1. **Merge conflicts**: Two documents declare the same URI as `owl:Class` with
   different `rdfs:subClassOf` parents. Options: last-write-wins, explicit
   conflict node (`dg:ConflictingDefinition`), or require user resolution.

2. **Templates: breadcrumb policy**: Should expansion emit a
   `<anchor> tpl:wasInstantiatedFrom tpl:Foo` triple alongside the lowered
   Part 2 so the inspector can fold-back without running a recognizer pass
   over the whole graph? Costs one extra triple per template-instance; saves
   running subgraph isomorphism against every registered template at display
   time. Probably yes for instance-form templates (anchor node is natural),
   unclear for pattern-form templates (no anchor).

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
