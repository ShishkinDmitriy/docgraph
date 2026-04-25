# DocGraph — Architecture Design Notes

> Session date: 2026-04-15. Last updated: 2026-04-25 (switched upper ontology from ISO 15926 Part 2 to Part 14). Read this file at the start of any session continuing this design.

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

Each source document gets its own named-graph TTL file. A registry tracks all sources.

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

1. Parse the TTL.
2. Sanity-check: does it reuse `lis:` URIs correctly? Does anything collide with already-
   defined URIs in other graphs?
3. Write into `graphs/<slug>.ttl` and register in `sources.ttl`.

This means:
- The existing `data/financial_documents.ttl` can be ingested via `docgraph add` as a
  bootstrap — becoming the first real test of the meta-ontology alignment.
- Users can author ontology files by hand and add them the same way.
- The system is symmetric: hand-written TTL and LLM-extracted TTL are both first-class.

---

## Extraction pipeline (PDF / text sources)

```
docgraph add invoice.pdf
    │
    ├─ [if .ttl / .n3 / .jsonld / .trig]
    │   Parse → load into named graph
    │   Done — no LLM
    │
    └─ [if .pdf / .txt / .md / ...]
        │
        ├─ Pass 0: PDF → Markdown (existing classifier.py)
        │
        ├─ Pass 1: concept extraction
        │   "What are the main objects/concepts in this document?"
        │   Returns: [{label, description, raw_context_snippet}, ...]
        │
        ├─ Pass 2: meta-classification per concept
        │   For each concept:
        │   - Which lis:* class does it map to?
        │     (lis:InformationObject  — a concrete document/record instance, OR a new
        │                                 OWL class subClassOf lis:InformationObject for
        │                                 a document *type*
        │      lis:Activity            — an event / process step
        │      lis:Person, lis:Organization, lis:Location — actors and places
        │      lis:PhysicalObject      — physical things
        │      lis:Quality / Role / Function / Disposition — aspects
        │      owl:ObjectProperty / owl:DatatypeProperty — a relation/attribute type)
        │   - Is this an INSTANCE (rdf:type) or a TYPE (a new OWL class)?
        │   - Does this document DEFINE it or REFERENCE it?
        │   - If property: what is its modality (dg:Mandatory/Preferred/Optional/Prohibited)?
        │   - If property: what are its rdfs:domain and rdfs:range?
        │
        ├─ Pass 3: resolution against existing graphs
        │   DEFINE → mint URI, write to this document's named graph
        │   REFERENCE → fuzzy-match against URIs in existing graphs
        │             → if no match: add stub to graphs/_unresolved.ttl
        │
        └─ Pass 4: instance property extraction (for individuals only)
            Use modality triples on the matched class's properties to guide extraction
            Mandatory  → must find value
            Optional   → attempt
            Prohibited → skip
            (This replaces the current agent.py extraction loop)
```

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
