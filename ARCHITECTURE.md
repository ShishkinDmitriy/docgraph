# DocGraph — Architecture Design Notes

> Session date: 2026-04-15. Last updated: 2026-04-27 (unified ingest pipeline: format-specific parsers + common analyzer; rules-as-data normalization; "what does this document define?" replaces binary `detectedRole`). Read this file at the start of any session continuing this design.

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

## Pipeline shape: format-specific extraction + uniform analyzer

Every ingest is one shape regardless of input format. Format-specific parsers do
the front half — turn the source into candidate triples in the source's own
vocabulary. A uniform **analyzer** does the back half — detect what was
defined, normalize non-canonical idioms, anchor classes to Part 14, emit the
named graph.

```
                    ┌────────── format-specific ──────────┐  ┌──────────────────── uniform analyzer ────────────────────┐
                    │                                      │  │                                                            │
PDF (any kind) ───► │  PDF → Markdown (cached)             │  │  Phase 1: detect what the source defines                  │
                    │       └► vision LLM extract triples ─┼──┼─►        (classes? properties? individuals?)                │
                    │                                      │  │                                                            │
TTL (any kind) ───► │  parse                              ─┼──┼─►  Phase 2: normalize non-canonical idioms                  │
                    │                                      │  │           (lift rules — see "Analyzer pipeline")           │
                    │                                      │  │                                                            │
… future formats ─► │  …                                  ─┼──┼─►  Phase 3: anchor declared classes to ISO 15926 Part 14    │
                    │                                      │  │                                                            │
                    └──────────────────────────────────────┘  │  Phase 4: emit named graph + register in sources.ttl       │
                                                              │                                                            │
                                                              └────────────────────────────────────────────────────────────┘
```

Two consequences:

- **TTL doesn't "skip" extraction** — it has *cheap* extraction (parsing) instead
  of expensive (PDF → MD → vision LLM). Everything from Phase 1 rightward is
  identical.
- **Convergence is approximate, not bit-identical.** A PDF describing schema.org
  and the schema.org TTL itself ingest to *similar* graphs, not identical: the
  PDF path is lossier and has to resolve URIs ("an Invoice…" → which Invoice?)
  the TTL gets for free. Useful as a test target — "the two should overlap on
  ≥ N% of classes/properties" — not a guaranteed equality.

### What can a document define?

After extraction, Phase 1 of the analyzer asks three independent yes/no
questions and records the answers as `dg:defines` triples:

| Question | Stored as | Triggered by |
|---|---|---|
| Defines classes? | `<source> dg:defines dg:Classes` | `?x a owl:Class`, `rdfs:Class`, `skos:Concept`, … |
| Defines properties? | `<source> dg:defines dg:Properties` | `?x a owl:ObjectProperty`, `owl:DatatypeProperty`, `rdf:Property`, … |
| Defines individuals? | `<source> dg:defines dg:Individuals` | `?x a <some-class-not-in-the-meta-vocabulary>` |

Any combination is valid. An ontology TTL with named individuals → all three.
A receipt PDF → `dg:Individuals` only. A standards PDF defining what an
Invoice is → `dg:Classes` and `dg:Properties` (and possibly some illustrative
individuals).

This **replaces** the earlier binary `dg:detectedRole = DefinesTypes |
AssertsInstances` — the binary was too narrow.

### Subject (Q1) and form (Q2) still apply, separately

The earlier classification questions are orthogonal to the structural "what
does it define" axis. A document that *defines* `schema:Invoice` is not the
same as a document that *is* an instance of `schema:Invoice`. Q1/Q2 answer the
latter; the "defines" axis answers the former. Both can apply to the same
source. See "Classification" below for Q1/Q2 details.

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

### TTL ingest is one parser among several

A `.ttl` source goes through the same pipeline as any other input: parse →
analyzer (Phase 1–4) → named graph. The TTL parser is just *cheaper* than the
PDF parser (no vision LLM step). For pure-OWL TTLs like
`data/financial_documents.ttl`, Phase 2 (normalization) and Phase 3 (Part 14
anchoring) are no-ops — the source already uses canonical predicates and roots
under `lis:`. For schema.org or SKOS, Phase 2 rewrites idioms via lift rules
(see "Analyzer pipeline" below).

The ingest stamps the registry with `dg:addedAt` and one or more `dg:defines`
values determined by Phase 1 (Classes, Properties, Individuals).

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

### Graph files are real files

Regardless of input format, `graphs/<slug>.ttl` is a real file written by the
analyzer (Phase 4) — never a symlink to the source. The analyzer's output is
the *normalized view* (Phase 2 rewrites + Phase 3 anchors + canonical triples
the source already had), and that view is rarely byte-identical to the source.
Storing it as a real file lets cascade-delete drop it cleanly without touching
the user's original input.

The original TTL/PDF source stays where the user put it; the registry
references it by path, but the graph is ours.

### sources.ttl example

```turtle
@prefix lis: <http://standards.iso.org/iso/15926/part14/> .
@prefix dg:  <http://example.org/docgraph/meta#> .

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
`dg:Classes`, `dg:Properties`, `dg:Individuals` are docgraph-specific (no Part 14
equivalent for ingestion metadata).

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

## Analyzer pipeline (Phase 1–4)

The analyzer is the format-agnostic back half of every ingest. It runs once
per source, after the format-specific parser has produced candidate triples in
the source's own vocabulary.

### Phase 1 — detect what the source defines

Walk the candidate triples. Answer the three "defines" questions (Classes,
Properties, Individuals) by structural inspection — no LLM call needed:

```
declares ?x a owl:Class | rdfs:Class | skos:Concept …  →  dg:Classes
declares ?x a owl:ObjectProperty | DatatypeProperty | rdf:Property …  →  dg:Properties
declares ?x a <C>, where <C> is not in the meta-vocabulary  →  dg:Individuals
```

Emit `<source> dg:defines …` triples. This drives which subsequent phases need
to run: a pure instance document skips Phase 2/3 (no classes to normalize or
anchor); a pure ontology skips downstream individual-extraction.

### Phase 2 — normalize non-canonical idioms

For every declared class and property, check whether its **structural slots**
are filled with canonical predicates:

- A property declared without `rdfs:domain`/`rdfs:range` but *with*
  `schema:domainIncludes` or similar → idiom needs a lift rule.
- A class declared without `rdfs:subClassOf` parent but *with* `skos:broader`
  → same.
- A `rdf:Property` declaration with no `owl:DatatypeProperty`/`ObjectProperty`
  typing, where the range determines which → same.

Pure-OWL inputs have all slots filled canonically and Phase 2 is a no-op. The
detection is automatic — the user doesn't declare "this needs normalization",
the analyzer finds it by inspection.

For each idiom predicate that triggered the signal, the analyzer looks up a
**lift rule** (a SPARQL CONSTRUCT) in two locations:

```
data/normalization/         ← pre-seeded rules shipped with docgraph
  schemaorg.rq              ← schema:domainIncludes/rangeIncludes/property typing
  skos-as-taxonomy.rq       ← skos:broader/narrower → rdfs:subClassOf
.docgraph/cache/lifts/      ← runtime-discovered rules (LLM-generated, user-approved)
  <predicate-slug>.rq
```

Both locations are equal-status. The loader unions all matching rules. There
is no "deterministic vs LLM" split in code — pre-seeded entries are just LLM-
work-already-done-at-build-time, in the same on-disk format the runtime cache
uses. Users can override or delete pre-seeded entries.

If a non-canonical idiom has no rule in either location, Phase 2 prompts the
LLM with the predicate URI and its `rdfs:label`/`comment` from the source, and
asks for a CONSTRUCT-shaped rewrite (or "pass through" if it was already
canonical and Phase 2's heuristic was wrong). Output is shown to the user for
approval, then cached in `.docgraph/cache/lifts/`. Cache key is the predicate
URI — predicate semantics are vocabulary-stable, so the same predicate seen in
the next ingest reuses the rule.

### Phase 3 — anchor declared classes to Part 14

For every class declared in the (now-normalized) source, walk
`rdfs:subClassOf*` upward. If it terminates at any `lis:` class → no anchor
needed. If it doesn't, send to the LLM with the Part 14 catalogue (reusing the
Q1 prompt material — ~30 classes) and get back one of:

- `<class> rdfs:subClassOf lis:<X>` — the closest-fit Part 14 superclass.
- `<class> dg:noPartFourteenAnchor true` — class has no Part 14 home (e.g.,
  `schema:PaymentMethod`, an intangible classifier); leave it unrooted.

User reviews. Cached per class URI in `.docgraph/cache/anchors/`. Anchoring
permits "no anchor" rather than forcing every class up to `lis:Object` —
otherwise the hierarchy fills with noise.

### Phase 4 — emit named graph

Write the normalized graph (Phase 2 rewrites + Phase 3 anchors + everything
the source already declared canonically) to `graphs/<slug>.ttl` and register
in `sources.ttl`. Cascade-delete drops the file and the registry entry.

### Caching summary

Two long-lived caches survive source removal — they're vocabulary-level
facts, not document-level:

```
.docgraph/cache/lifts/<predicate-slug>.rq    ← per-predicate lift rule
.docgraph/cache/anchors/<class-slug>.ttl     ← per-class Part 14 anchor
```

Same shape as the PDF→Markdown cache (cache the expensive LLM work so it
doesn't re-run), different key. `docgraph forget-rule <uri>` evicts an entry
that was approved in error.

### Bootstrap

`data/financial_documents.ttl` is the canonical Phase-2/3 no-op test:
ingesting it should produce a normalized graph byte-equivalent to the source
modulo blank-node renaming. If it doesn't, the analyzer is over-rewriting.

---

## Classification — two questions (Q1 + Q2)

Classification of an ingested document splits into two independent questions
asked in order. They have different scopes, different candidate sets, and
different cost profiles.

These are orthogonal to the structural axis introduced in "Pipeline shape" —
*what does this document define?* (Classes / Properties / Individuals). Q1/Q2
ask about the document's subject and form. The structural axis runs in the
analyzer (Phase 1) by inspecting triples; Q1/Q2 are LLM-driven semantic
calls. Both result sets land on the same `<source>` IngestionRecord but
answer different questions.

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

## Extraction pipeline (full sequence)

The unified pipeline that "Pipeline shape" introduces, with concrete steps:

```
docgraph add <file>
    │
    ├─ 0. Validate, hash for idempotency, check existing entry.
    │
    ├─ 1. Register file as lis:InformationObject + prov:Entity
    │     (file metadata: hash, size, mime, pdfinfo: pages, title, ...).
    │
    ├─ 2. Format-specific extraction (front half).
    │     ├─ [.ttl / .n3]  Parse → candidate triples (the source's own vocab).
    │     └─ [.pdf]        PDF → Markdown via Claude vision (cached) →
    │                      LLM extracts candidate triples from the Markdown.
    │                      Both PDF→MD and the extract are recorded as
    │                      prov:Activity in the default graph.
    │
    ├─ 3. Analyzer Phase 1 — what does this source define?
    │     Structural inspection of candidate triples. Emit
    │     <source> dg:defines dg:Classes/Properties/Individuals.
    │
    ├─ 4. Analyzer Phase 2 — normalize non-canonical idioms.
    │     For each declared class/property with empty structural slots,
    │     look up lift rules in data/normalization/ + cache/lifts/, prompt
    │     LLM if missing, apply. Pure-OWL inputs are no-ops.
    │
    ├─ 5. Analyzer Phase 3 — anchor declared classes to Part 14.
    │     For each class without a lis: ancestor, LLM picks closest fit
    │     from Part 14 catalogue (or "no anchor"). Cached per class URI.
    │     Skipped if Phase 1 found no Classes.
    │
    ├─ 6. Q1 — Subject identification (LLM, semantic).
    │     Candidates: ~30 Part 14 classes, sent in full.
    │     Emit <source> dg:isAbout <UpperClass>, …  Always runs.
    │
    ├─ 7. Q2 — Form classification (LLM, semantic; only when domain ontology loaded).
    │     Candidates: leaves of user-ingested ontologies.
    │     If ≥ 30: embedding top-k pre-filter; else send all.
    │     Emit <source> rdf:type <FormClass> in the extraction graph.
    │     Skipped (with clear message) when no domain ontology is loaded.
    │
    ├─ 8. Property extraction (only when Q2 ran).
    │     For the chosen form class, walk rdfs:subClassOf* ancestors and
    │     collect every property whose rdfs:domain matches. Single LLM
    │     call returns nested JSON; we mint URIs for object-typed
    │     properties (one level deep), emit triples into the extraction
    │     named graph. Coverage signal: filled-direct / total-direct.
    │
    └─ 9. Analyzer Phase 4 — emit named graph and register in sources.ttl.
```

Steps 3–5 are the analyzer's class/property work; steps 6–8 are subject/form
classification and per-document property extraction. They share the same
named graph (`<ext/<slug>>` for the extraction graph; `graphs/<slug>.ttl` for
the normalized source view).

The extraction graph is described as a `prov:Entity` in the default graph
and generated by all the LLM activities above (Phase 2 normalization, Phase
3 anchoring, Q1, Q2, property extraction). See "Provenance via named graphs"
above for the cascade story.

---

## What `docgraph init` produces

After init, `.docgraph/` contains only:

```
meta.ttl       ← imports ISO 15926 Part 14 + declares dg: extensions
                 (dg:Modality, dg:Mandatory/Preferred/Optional/Prohibited, dg:modality,
                  dg:status, dg:Unresolved, dg:IngestionRecord,
                  dg:defines, dg:Classes/Properties/Individuals,
                  dg:noPartFourteenAnchor, etc.)
sources.ttl    ← empty registry
graphs/        ← contains only an empty _unresolved.ttl
cache/
  pdfmd/       ← PDF → Markdown cache (per-document, key = doc hash)
  lifts/       ← LLM-discovered lift rules (per-predicate, key = predicate URI)
  anchors/     ← LLM-discovered Part 14 anchors (per-class, key = class URI)
```

No `financial_documents.ttl`. No domain classes. The graph is empty except for structure.
When the combined graph is loaded, `meta.ttl`'s `owl:imports` brings in Part 14 and the
~30-class hierarchy is available for classification.

### Pre-seeded normalization rules (shipped with docgraph, not in `.docgraph/`)

The repo ships a small set of lift rules for common vocabularies under
`data/normalization/`:

```
data/normalization/
  schemaorg.rq        ← schema:domainIncludes/rangeIncludes, rdf:Property typing
  skos-as-taxonomy.rq ← skos:broader/narrower → rdfs:subClassOf
```

These are pre-seeded equivalents of `cache/lifts/` entries — same on-disk
format, same code path. The user pays no LLM cost for first-time ingest of
schema.org or SKOS-shaped vocabularies; everything else still flows through
the LLM-discovered route.

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

7. **Existing `financial_documents.ttl`**: ingest via the new analyzer pipeline.
   Should be a Phase-2/Phase-3 no-op (already canonical OWL, already roots under
   `lis:`). The bootstrap test for "the analyzer doesn't over-rewrite".

8. **LLM rule approval flow**: Phase 2 lift discovery and Phase 3 anchor discovery
   both want user review before caching. Bundle into one combined diff at end of
   ingest ("here's how I translated this source — accept / edit / abort") or two
   separate prompts? Probably one combined diff.

9. **Pre-seeded vs cached rule conflict**: if a user runs `docgraph add` on a
   schema.org TTL, gets the pre-seeded lift, later edits `cache/lifts/` to
   override, then a docgraph upgrade ships a new pre-seeded version — whose wins?
   Probably the cache (it's user-owned), with a `docgraph diagnose` command to
   surface the divergence.

10. **"No anchor" surface**: `dg:noPartFourteenAnchor true` is queryable but
    noisy (every Part-14-foreign class carries the annotation). Alternative:
    silent (just leave class unrooted) and derive the "outside Part 14" set with
    a SPARQL query. Convenience-vs-cleanliness call.

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
