# DocGraph — Architecture Design Notes

> Session date: 2026-04-15. Last updated: 2026-04-27 (added Q1/Q2 classification design; pipeline outline updated to match current implementation). Read this file at the start of any session continuing this design.

## Vision

The current codebase is a financial-document extractor with a hardcoded ontology
(`financial_documents.ttl`). The goal is to make it fully general:

- **`docgraph init`** seeds only a meta-ontology — no domain classes.
- **`docgraph add <file>`** — the LLM figures out what kind of document it is and builds
  the knowledge graph accordingly.
- **`docgraph remove <file>`** — cascades: removes concepts the document defined, and
  degrades any individuals previously classified under those concepts to bare
  `lis:InformationObject` (unclassified, but not lost).

The result after adding three documents — a German invoice, an EU standard defining
Invoice, and a meta-document classifying types of standards — should be a graph with:
- a class `:Invoice rdfs:subClassOf lis:InformationObject`, defined in the EU standard's
  named graph
- an individual for the invoice itself, typed as `:Invoice` in its own named graph
- meta-classification triples from the third document in yet another named graph

Removing the EU standard cascades: the `:Invoice` class definition disappears, and the
individual's `rdf:type :Invoice` triple is rewritten to `rdf:type lis:InformationObject`
(unclassified, but not lost).

---

## Meta-ontology — ISO 15926 Part 14 (strict alignment)

The meta-ontology **is** ISO 15926 Part 14, not merely inspired by it. All meta-classes
must use actual Part 14 class names and URIs. Custom classes must not be invented where
a Part 14 class already covers the concept.

Part 14 is an OWL 2 DL rendering of the ISO 15926-2 data model. The choice of Part 14
over Part 2 is deliberate: Part 14 is OWL-native (uses `rdf:type` and `rdfs:subClassOf`
directly, no reification of classification/specialization, no metaclass machinery) and
is far smaller — under 30 classes covering the same conceptual ground that Part 2 spreads
across 100+. This makes it dramatically easier to work with from standard OWL tooling
without losing semantic alignment.

### Why strict alignment matters

- Interoperability: graphs produced by docgraph can be consumed by any ISO 15926-aware
  tool without translation.
- Discipline: Part 14's vocabulary covers the structural concepts we need; inventing
  parallel concepts creates confusion.
- Future-proofing: when the standard adds concepts, we inherit them for free.

### Official OWL representation

The Part 14 ontology ships as Turtle locally at `docs/LIS-14.ttl` (READI 2020-09
deliverable, revised 2019-03-25, version IRI
`http://standards.iso.org/iso/15926/part14/1.0`).

Base namespace (the `lis:` prefix):
```
http://standards.iso.org/iso/15926/part14/
```

Note the trailing slash — Part 14 uses slash-separated IRIs (`lis:InformationObject` =
`http://standards.iso.org/iso/15926/part14/InformationObject`), not hash fragments. The
ontology IRI itself (`http://standards.iso.org/iso/15926/part14`) has no trailing slash.

`meta.ttl` should `owl:imports` `docs/LIS-14.ttl` (or load it as a local secondary
ontology) so the full Part 14 class hierarchy is available in the combined graph without
any network fetch. The `lis:` prefix maps to the namespace above.

### Core Part 14 hierarchy relevant to docgraph

Part 14's top-level structure splits everything into three disjoint roots:
`lis:Object` (3D things), `lis:Activity` (4D occurrences), and `lis:Aspect`
(qualities, dispositions, roles).

```
lis:Object                           top of the 3D side
  lis:InformationObject              ← documents, records (concrete instances)
    lis:QuantityDatum
      lis:ScalarQuantityDatum
    lis:UnitOfMeasure
      lis:Scale
  lis:PhysicalObject
    lis:InanimatePhysicalObject  (lis:Phase, lis:Stream)
    lis:Organism (lis:Person)
    lis:Compound, lis:Feature
  lis:FunctionalObject (lis:System)
  lis:Location (lis:SpatialLocation, lis:Site)
  lis:Organization

lis:Activity                         4D occurrences
  lis:Event (lis:PointInTime)
  lis:PeriodInTime

lis:Aspect                           inhering qualities, etc.
  lis:Quality (lis:PhysicalQuantity)
  lis:Disposition (lis:Function)
  lis:Role
```

Key relations Part 14 already provides:
`lis:representedBy` (any thing → `lis:InformationObject`), `lis:hasParticipant`,
`lis:hasRole`, `lis:hasFunction`, `lis:hasQuality`, `lis:hasPart` (and its
specialisations `hasArrangedPart`, `hasFunctionalPart`, etc.), the temporal `before` /
`after` / `causes`, and the connectivity `connectedTo`.

### Classes central to docgraph's information model

```turtle
@prefix lis: <http://standards.iso.org/iso/15926/part14/> .

lis:InformationObject   # superclass for every document and record we ingest
```

A specific German invoice document is an *individual* of type `lis:InformationObject`.
A document *type* like "Invoice" is an OWL class with
`rdfs:subClassOf lis:InformationObject`. Classification is plain `rdf:type`; sub-typing
is plain `rdfs:subClassOf`. There is no `ClassOfInformationObject` metaclass in
Part 14 — there doesn't need to be.

### What Part 14 does *not* model — the `dg:` extension namespace

Part 14 omits a few things docgraph needs:

| Concept | Part 14 status | docgraph approach |
|---|---|---|
| Modality (MUST / SHOULD / MAY / MUST NOT) | Not modelled | `dg:Modality` class with four instances |
| Provenance / source ownership | Not modelled | named graphs + `dg:` ingestion metadata |
| Unresolved-stub status | Not modelled | `dg:status dg:Unresolved` |

The `dg:` namespace (`http://example.org/docgraph/meta#`) is reserved for these
docgraph-specific additions. Every structural class must come from `lis:` if Part 14
covers it.

### Built-in modality individuals (RFC 2119 as docgraph individuals)

Baked into `meta.ttl`. They represent the normative modality vocabulary from RFC 2119 /
ISO drafting directives. Since Part 14 has no metaclass-of-relationship concept, modality
is simply a docgraph enumeration:

```turtle
@prefix dg: <http://example.org/docgraph/meta#> .

dg:Modality    a owl:Class .

dg:Mandatory   a dg:Modality .  # MUST / SHALL
dg:Preferred   a dg:Modality .  # SHOULD
dg:Optional    a dg:Modality .  # MAY
dg:Prohibited  a dg:Modality .  # MUST NOT

dg:modality    a owl:ObjectProperty ;
    rdfs:range  dg:Modality .   # attaches to a property to indicate its modality
```

---

## Provenance via named graphs (replaces Part 2 reification)

ISO 15926-2 reified every relationship so provenance, temporal scope, and jurisdiction
could attach to the relationship itself. Part 14 drops reification in favour of standard
OWL. Docgraph follows suit and uses **named graphs** as the unit of provenance:

- Every triple lives in exactly one named graph.
- Each ingested document owns one named graph (`graphs/<slug>.ttl`).
- The graph URI *is* the source identifier — no per-triple `dg:definedBy` needed.
- The permanent meta-ontology backbone lives in `meta.ttl` (its own graph).
- Cascade-delete = drop the document's named graph + repair dangling type references in
  the remaining graphs.

### Permanent backbone — `meta.ttl`

`meta.ttl` is the structural scaffolding written once by `init` and never overwritten. It
loads Part 14 and declares the docgraph-specific extensions:

```turtle
# meta.ttl — permanent scaffolding
@prefix lis:  <http://standards.iso.org/iso/15926/part14/> .
@prefix dg:   <http://example.org/docgraph/meta#> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .

<http://example.org/docgraph/meta>  a owl:Ontology ;
    owl:imports <http://standards.iso.org/iso/15926/part14> .

dg:Modality   a owl:Class .
dg:Mandatory  a dg:Modality .
dg:Preferred  a dg:Modality .
dg:Optional   a dg:Modality .
dg:Prohibited a dg:Modality .
dg:modality   a owl:ObjectProperty ; rdfs:range dg:Modality .
```

### Document-sourced assertions

When a document asserts that "Invoice is a subtype of InformationObject" or that
"this invoice IS an Invoice", these are plain OWL triples written into the document's
named graph:

```turtle
# graphs/eu-standard.ttl — named graph for the EU standard
@prefix lis: <http://standards.iso.org/iso/15926/part14/> .
@prefix dom: <http://example.org/docgraph/domain/> .

dom:Invoice  a owl:Class ;
    rdfs:subClassOf lis:InformationObject ;
    rdfs:label "Invoice" .

dom:hasVatNumber  a owl:DatatypeProperty ;
    rdfs:domain dom:Invoice ;
    rdfs:range  xsd:string ;
    dg:modality dg:Mandatory .

# graphs/german-invoice.ttl — named graph for the invoice document
<doc/invoice-001>  a dom:Invoice ;
    dom:hasVatNumber "DE123456789" .
```

Provenance, temporal scope, and jurisdiction (when needed) attach to the *named graph*,
not to individual triples. The registry (`sources.ttl`) carries this metadata:
`dg:addedAt`, `dg:validFrom`, `dg:scope`, etc.

### Cascade delete

`docgraph remove eu-standard.pdf`:

1. Look up the graph file in `sources.ttl` → `graphs/eu-standard.ttl`.
2. Parse it; collect every class and property URI defined there (subjects with
   `rdf:type owl:Class`, `owl:ObjectProperty`, or `owl:DatatypeProperty`).
3. Show the user what will be removed (concepts + dependent individuals).
4. On confirm: delete the graph file and remove its registry entry.
5. Scan the remaining named graphs for triples whose predicate or `rdf:type` referenced
   a now-undefined concept:
   - `<x> rdf:type <removed-class>` → rewrite to `rdf:type lis:InformationObject`
     (if the removed class was a subclass of `lis:InformationObject`) or remove the
     triple otherwise.
   - `<x> <removed-property> _` → remove the triple.

The meta backbone (`meta.ttl`) is never touched.

### Translating precompiled TTL files

A hand-authored `.ttl` source uses OWL constructs natively (`rdfs:subClassOf`, `rdf:type`,
`owl:ObjectProperty`). Because Part 14 is also OWL-native, ingest is **a straight load**
into the source's named graph — no translation, no reification step. The source becomes
its own named graph and cascade-deletes cleanly.

The ingest does still need to:
- Resolve any new domain classes against existing concepts (DEFINE vs REFERENCE — see
  next section).
- Stamp the registry with `dg:addedAt` and a `dg:detectedRole` (does this source mostly
  define types, or assert instances?).

---

## DEFINE vs REFERENCE — ownership

For every concept the system encounters in a document, the LLM (or the TTL ingester) must
decide:

| Relationship | Meaning | Lifecycle |
|---|---|---|
| Concept defined in this document's graph | This document is the normative source | Remove doc → drop the graph → concept gone |
| Concept referenced but defined elsewhere | This document uses, doesn't own | Remove doc → no effect on the concept |

With named-graph provenance, ownership is *positional*: a concept is defined by whichever
graph contains its declaration triple (`a owl:Class` plus `rdfs:subClassOf …`). A
referencing document just uses the URI without redeclaring it.

When ambiguity arises (the same URI appears with `a owl:Class` in two graphs), it's a
merge conflict — see open questions below.

### Unresolved concepts

If a document references a concept that has no defining document yet, we can't simply
omit it — we lose the reference. Instead, the ingester mints a **stub** in a dedicated
`graphs/_unresolved.ttl` graph:

```turtle
# graphs/_unresolved.ttl
dom:Invoice  a lis:InformationObject ;
    dg:status         dg:Unresolved ;
    dg:firstSeenIn    <source/german-invoice.pdf> .
```

A stub is typed as plain `lis:InformationObject` (no subclass relationship yet) and
flagged `dg:Unresolved`. When a defining document is later added, the loader:

1. Detects that the new graph defines `dom:Invoice` (i.e., contains
   `dom:Invoice a owl:Class ; rdfs:subClassOf …`).
2. Removes the stub triples from `_unresolved.ttl`.
3. Optionally rewrites individuals in other graphs that were typed as
   `lis:InformationObject` but referenced through `dom:Invoice` to use the now-defined
   class.

This makes the **order of ingestion irrelevant** — documents can be added in any order
and the graph heals itself.

`dg:status`, `dg:Unresolved`, and `dg:firstSeenIn` are docgraph-specific (no Part 14
equivalent for ingestion bookkeeping).

---

## Modality and SHACL derivation

Modality is extracted directly from normative text and stored as triples on the property
declaration, in the defining document's named graph:

```turtle
# graphs/eu-standard.ttl — extracted from "The Seller VAT identifier MUST be present"
dom:hasVatNumber  a owl:DatatypeProperty ;
    rdfs:label  "VAT Number" ;
    rdfs:domain dom:Invoice ;
    rdfs:range  xsd:string ;
    dg:modality dg:Mandatory .

# "The buyer reference SHOULD be provided"
dom:hasBuyerRef  a owl:DatatypeProperty ;
    rdfs:domain dom:Invoice ;
    rdfs:range  xsd:string ;
    dg:modality dg:Preferred .
```

Compared to the previous Part 2 design, this is dramatically simpler: no reified
`Classification` individuals, no `ClassOfClassOfRelationship` chain. Just an OWL property
with one extra annotation.

### SHACL as a derived view

SHACL shapes are **not stored** — they are generated on demand from modality triples:

```python
def derive_shacl(graph):
    for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
        modality = graph.value(prop, DG.modality)
        if modality is None:
            continue
        domain = graph.value(prop, RDFS.domain)
        range_ = graph.value(prop, RDFS.range)
        if modality == DG.Mandatory:
            yield NodeShape(targetClass=domain, path=prop, minCount=1, datatype=range_)
        elif modality == DG.Prohibited:
            yield NodeShape(targetClass=domain, path=prop, maxCount=0)
```

Removing the defining document drops its named graph → modality triples vanish → derived
shapes change automatically.

---

## Storage layout (file-based, no triplestore yet)

**One source document → one TTL file.** Each source gets its own named-graph TTL file
under `graphs/` so the result is easy to inspect by eye. A registry tracks all sources.

```
.docgraph/
  meta.ttl             ← imports Part 14 + dg: extensions (written by `init`, never overwritten)
  sources.ttl          ← registry: source path → graph file → added date, detected role
  graphs/
    _unresolved.ttl    ← stubs for concepts referenced before they were defined
    <slug>.ttl         ← one file per source document (named graph)
  cache/               ← existing PDF-to-markdown cache (unchanged)
```

The `lis:` and `dg:` prefixes are pre-bound in every graph file for readability.

### TTL inputs: symlink, don't copy

When the source is already a TTL file, `graphs/<slug>.ttl` is a **symlink** to the
original, not a copy. Rationale (debugging convenience, not a permanent design choice):

- One file on disk — no copy/drift.
- Editing the original immediately changes the graph, which is what the user wants
  while the meta-ontology is being shaped.
- Cascade-delete just unlinks the symlink; the original is untouched.

Caveats to revisit if/when this becomes a problem:
- If the original file is moved or deleted, the link dangles. `remove` and `status`
  must detect this.
- Edits to the original have no provenance trail. Acceptable while iterating; switch to
  copy-on-ingest before this is shipped to anyone else.

For PDF / Markdown / other non-TTL inputs, `graphs/<slug>.ttl` is a real file written by
the extraction pipeline.

### sources.ttl example

```turtle
@prefix lis: <http://standards.iso.org/iso/15926/part14/> .
@prefix dg:  <http://example.org/docgraph/meta#> .

<source/eu-standard.pdf>  a dg:IngestionRecord ;
    dg:sourcePath   "eu-standard.pdf" ;
    dg:graphFile    ".docgraph/graphs/eu-standard.ttl" ;
    dg:addedAt      "2026-04-15"^^xsd:date ;
    dg:detectedRole dg:DefinesTypes .              # this source mostly defines classes

<source/german-invoice.pdf>  a dg:IngestionRecord ;
    dg:sourcePath   "german-invoice.pdf" ;
    dg:graphFile    ".docgraph/graphs/german-invoice.ttl" ;
    dg:addedAt      "2026-04-15"^^xsd:date ;
    dg:detectedRole dg:AssertsInstances .          # this source is an instance document
```

`dg:IngestionRecord`, `dg:sourcePath`, `dg:graphFile`, `dg:addedAt`, `dg:detectedRole`,
`dg:DefinesTypes`, `dg:AssertsInstances` are docgraph-specific (no Part 14 equivalent for
ingestion metadata).

### Cascade delete

`docgraph remove <file>`:
1. Look up the graph file in `sources.ttl`.
2. Parse it; collect every class and property URI it declares.
3. Show the user what will be removed (concepts + dependents).
4. On confirm: delete the graph file, remove from `sources.ttl`.
5. Scan all other graph files for triples that reference the removed URIs and repair
   them (rewrite type to `lis:InformationObject` or drop the triple, per the rules
   above).

---

## TTL files as precompiled sources

A `.ttl` source **skips LLM extraction entirely** — parsed and loaded into a named graph
at ingest time. Same provenance and cascade semantics as PDF-derived graphs.

Because Part 14 is OWL-native, hand-authored OWL TTL maps directly onto our model — no
translation step is needed. Ingest:

1. Parse the TTL (just to sanity-check it loads and to extract declared URIs).
2. Sanity-check: does it reuse `lis:` URIs correctly? Does anything collide with already-
   defined URIs in other graphs?
3. Symlink `graphs/<slug>.ttl` → original file (see "TTL inputs: symlink, don't copy"
   above) and register in `sources.ttl`.

This means:
- The existing `data/financial_documents.ttl` can be ingested via `docgraph add` as a
  bootstrap — becoming the first real test of the meta-ontology alignment.
- Users can author ontology files by hand and add them the same way.
- The system is symmetric: hand-written TTL and LLM-extracted TTL are both first-class.

---

## Classification — two questions (Q1 + Q2)

Classification of an ingested document splits into two independent questions
asked in order. They have different scopes, different candidate sets, and
different cost profiles.

### Q1 — Subject: what is this document *about*?

- **Stored as**: `<source> dg:isAbout <UpperClass>, …` (zero or more values).
- **Candidate scope**: **ISO 15926 Part 14 classes only.**
  - PROV-O is intentionally excluded — we use it for *metadata/provenance*
    (`prov:Activity`, `prov:wasGeneratedBy`, …), not as a subject vocabulary.
    Including it would conflate "what the document is about" with "what
    happened during ingest."
  - DCMI Terms is also excluded — its classes overlap with Part 14 and
    introduce noise.
- **Set size**: ~30 classes. Cheap enough that we send the whole catalogue to
  the LLM with no embedding pre-filter. RAG is not used here.
- **Always runs**, regardless of whether a domain ontology is loaded. This is
  the question that's *always* answerable: every document is at least
  intuitively "about" something at the upper-ontology level (an Activity, an
  Object, a Person, an Organization, a Quality, …).
- **Examples**:
  - Zahnrechnung (dental invoice) → `dg:isAbout lis:Activity, lis:Person`
    (the dental procedure, the participants).
  - PROV-O ontology document → `dg:isAbout lis:Activity, lis:Object`
    (it defines activity/entity vocabulary).
  - Sensor reading → `dg:isAbout lis:Quality`.
  - Poetry book → `dg:isAbout lis:Object` (vague — and that vagueness is
    itself the "outside our domain" signal).
- **Doubles as the uncovered diagnostic**: if Q1 returns only the most
  generic subjects (`lis:Object` and nothing more specific) with low
  confidence, the document is outside the upper ontology's resolution.
  Replaces the earlier `dg:typeNearestSimilarity < 0.3` geometric heuristic
  with a semantically grounded one.

### Q2 — Form: what *kind of document* is this?

- **Stored as**: `<source> rdf:type <FormClass>` (single value).
- **Candidate scope**: leaf classes from **user-ingested ontologies only**.
  - "User-ingested" = declared in a named graph that came from
    `docgraph add <file>.ttl`. Bundled foundationals (Part 14, PROV-O, DCMI,
    docgraph meta) don't contribute form candidates — they're scaffolding,
    not subject matter. (If a user ingests Part 14 a second time
    deliberately, it joins the candidate pool — opting in is allowed.)
  - "Leaf" = no other class declares this as its `rdfs:subClassOf` parent
    in the combined dataset. Abstract intermediates like
    `fin:FinancialDocument` (which has 4 subclasses) are filtered out — the
    LLM should always pick the most specific class.
  - The leaf rule is structural; no per-class annotation is needed.
- **Set size**: variable. Small (5 in the toy financial example), large in
  real domain ontologies (200+ in a procurement RDL).
- **RAG as a count-based optimization**: when there are ≥ 30 candidates,
  the embedding store narrows to top-30 by cosine similarity before the LLM
  call; otherwise the candidate list is sent intact. Below 30 the prompt is
  cheap enough that filtering loses information without saving meaningfully.
- **Conditionally runs**: when no user ontology is loaded, Q2 is skipped
  with a clear message ("no domain ontology — `docgraph add <ontology.ttl>`
  first"), not an opaque "uncovered" gate.

### Why the form-vs-subject distinction matters

A common ontology-design mistake is to flatten form and event into the same
class hierarchy. The financial ontology in `data/financial_documents.ttl`
correctly keeps them separate — and is the model for how domain ontologies
should be authored:

```turtle
# Form branch — documents (subClassOf lis:InformationObject)
fin:FinancialDocument     rdfs:subClassOf lis:InformationObject .
fin:DemandForPayment      rdfs:subClassOf fin:FinancialDocument .
fin:ConfirmationOfPayment rdfs:subClassOf fin:FinancialDocument .
fin:Quote                 rdfs:subClassOf fin:FinancialDocument .
fin:Statement             rdfs:subClassOf fin:FinancialDocument .

# Event branch — financial activities (subClassOf prov:Activity ⊑ lis:Activity)
fin:Transaction  rdfs:subClassOf prov:Activity .
fin:Payment      rdfs:subClassOf fin:Transaction .
fin:Transfer     rdfs:subClassOf fin:Transaction .
fin:Payout       rdfs:subClassOf fin:Transaction .
```

A specific Zahnrechnung answers both questions from the right branches:
- Q1 (subject) → `dg:isAbout lis:Activity` — the underlying payment/treatment.
- Q2 (form)   → `rdf:type fin:DemandForPayment` — the layout/document kind.

If a domain ontology mixes the two — e.g., declares "Invoice" as both a form
and an event under one class — both questions return the same answer and the
distinction collapses. That's a *modelling* failure, not a pipeline failure.

### Q1 narrowing Q2 (deferred)

The natural follow-up question is whether Q1's answer can pre-filter Q2's
candidate set ("the document is about an Activity → consider only form
classes structurally related to Activity"). This is a real optimization for
projects with 100+ form classes, but requires a relevance-mapping mechanism
between forms and subjects. Three honest options when the time comes:

- Embedding affinity between form and subject `class_text`s.
- Property analysis: a form is relevant to a subject if any of its declared
  `rdfs:range`s reference the subject (or a transitive subclass).
- LLM-judged once at ontology-add: "for each form class, what upper-ontology
  subject is it most concerned with?" Tag as `dg:concernsSubject`.

For current scales (small handcrafted ontologies), independent Q1 + Q2 is
sufficient. The cascade is future work; the embedding store is already in
place to power option 1 when needed.

### Coverage signals

Per ingest, the default graph carries:

```turtle
<ext/<slug>>
    dg:subjectConfidence  0.81 ;            # Q1's headline confidence
    dg:typeConfidence     0.92 ;            # Q2's headline confidence (if Q2 ran)
    dg:typeCoverage       0.67 ;            # filled-direct-props / total (if Q2 ran)
    dg:typeNearestSimilarity 0.27 ;         # best Q2 cosine score (if Q2 ran)
    dg:isAbout            lis:Activity, lis:Person .  # Q1 result (in extraction graph)
```

Reading them together gives the diagnostics the user wants:
- High `subjectConfidence` + Q2 didn't run → "we know what it's about; you
  haven't loaded a form ontology yet."
- High `subjectConfidence` + low `typeNearestSimilarity` → "we know the
  general topic; no loaded form fits — the document is outside this
  project's domain coverage."
- High `subjectConfidence` + high `typeConfidence` + low `typeCoverage` →
  "right type, but document is sparse — many of the type's declared
  properties weren't in the document."

---

## Extraction pipeline (PDF / text sources)

```
docgraph add invoice.pdf
    │
    ├─ [if .ttl / .n3 ]
    │     Parse → symlink into graphs/<slug>.ttl → register in sources.ttl
    │     No LLM. No conversion. No classification.
    │
    └─ [if .pdf]
        ├─ 0. Validate, hash for idempotency, check existing entry
        ├─ 1. Register file as lis:InformationObject + prov:Entity
        │     (file metadata: hash, size, mime, pdfinfo: pages, title, ...)
        ├─ 2. Convert PDF → Markdown via Claude vision (cached)
        │     Register the markdown derivative + record the conversion
        │     as a prov:Activity in the default graph.
        ├─ 3. Q1 — Subject identification
        │     Candidates: Part 14 classes (~30, sent in full).
        │     Emit  <source> dg:isAbout <UpperClass>, ...
        │     Always runs.
        ├─ 4. Q2 — Form classification
        │     Candidates: leaves of user-ingested ontologies.
        │     If ≥ 30: embedding top-k pre-filter; else send all.
        │     Emit  <source> rdf:type <FormClass>  in the extraction graph.
        │     Skipped (with clear message) when no domain ontology is loaded.
        └─ 5. Property extraction
              For the chosen form class, walk rdfs:subClassOf* ancestors and
              collect every property whose rdfs:domain matches. Single LLM
              call returns a nested JSON; we mint URIs for object-typed
              properties (one level deep), emit triples into the extraction
              named graph. Coverage signal: filled-direct / total-direct.
```

The extraction graph is a separate named graph inside the source's TriG
file (`<ext/<slug>>`), described as a `prov:Entity` in the default graph and
generated by Q1's classify activity, Q2's classify activity, and the extract
activity. See "Provenance via named graphs" above for the cascade story.

---

## What `docgraph init` produces

After init, `.docgraph/` contains only:

```
meta.ttl    ← imports ISO 15926 Part 14 + declares dg: extensions
              (dg:Modality, dg:Mandatory/Preferred/Optional/Prohibited, dg:modality,
               dg:status, dg:Unresolved, dg:IngestionRecord, etc.)
sources.ttl ← empty registry
graphs/     ← contains only an empty _unresolved.ttl
cache/      ← empty
```

No `financial_documents.ttl`. No domain classes. The graph is empty except for structure.
When the combined graph is loaded, `meta.ttl`'s `owl:imports` brings in Part 14 and the
~30-class hierarchy is available for classification.

---

## Future: triplestore migration

Current plan uses **rdflib `Dataset`** with TriG/N-Quads format for named graphs, stored
as files. This is readable, version-controllable, and testable on small corpora.

When scale requires it, the file layout maps 1-to-1 to a triplestore's named graphs
(Oxigraph, Apache Fuseki). Migration path: replace file I/O with SPARQL HTTP client,
keep the same graph URI scheme.

---

## Open questions / next decisions

1. **ISO 15926 Part 14 mapping** *(resolved)*: Part 14's OWL 2 DL profile is the upper
   ontology. Key decisions:
   - Use `lis:` prefix for `http://standards.iso.org/iso/15926/part14/` (slash, not hash).
   - Document instances → `lis:InformationObject` (or a subclass).
   - Document types → OWL classes with `rdfs:subClassOf lis:InformationObject`.
   - Properties → `owl:ObjectProperty` / `owl:DatatypeProperty` with `rdfs:domain`/`range`.
   - Modality (Mandatory/Preferred/Optional/Prohibited) is docgraph-specific
     (`dg:Modality` enum) — Part 14 has no equivalent.
   - Provenance is the named graph, not a per-triple `dg:definedBy`.

2. **Prototype order**: TTL ingest first (proves meta-ontology structure, no LLM risk) or
   PDF role-detection first (proves the LLM pipeline)?

3. **`docgraph remove`**: Show diff of what will cascade before confirming?

4. **`docgraph status`**: Surface contents of `_unresolved.ttl` — "these concepts are
   referenced but have no defining document".

5. **Merge conflicts**: Two documents declare the same URI as `owl:Class` with different
   `rdfs:subClassOf` parents. Options: last-write-wins, explicit conflict node
   (`dg:ConflictingDefinition`), or require user resolution.

6. **Scope / temporal validity**: When a standard has a validity period or jurisdiction,
   attach it to the *named graph* (registry entry in `sources.ttl`), not to each triple.
   Confirm this is sufficient for the use cases on the table.

7. **Existing `financial_documents.ttl`**: Ingest as a precompiled TTL source — since
   Part 14 is OWL-native this is a straight load with no translation. First real test of
   the meta-ontology alignment.

---

## Current codebase reference

Key files before the redesign:

| File | Role in current system |
|---|---|
| `src/ontology.py` | Loads `docgraph.ttl`, builds combined graph, extracts `DocumentClass` list |
| `src/classifier.py` | PDF → Markdown (Pass 0) |
| `src/agent.py` | Main extraction agent loop (classify + extract in one pass) |
| `src/models.py` | `DocumentClass`, `ClassificationResult`, `DocumentHit` dataclasses |
| `src/project.py` | `docgraph init` — creates `.docgraph/` layout |
| `data/financial_documents.ttl` | Hardcoded domain ontology (to be replaced) |
| `data/docgraph.ttl` | Project registry (to be redesigned around sources.ttl) |
| `data/shapes.ttl` | Hand-authored SHACL shapes (to be derived from modality triples) |
