# DocGraph — Architecture Design Notes

> Session date: 2026-04-15. Last updated: **2026-05-02** (switched meta-ontology from ISO 15926 Part 14 back to **Part 2**; added the file → document → chapter → quote → referenced-individual chain; documented when to reify Classification/Specialization vs. when to use `rdf:type`/`rdfs:subClassOf` directly). Read this file at the start of any session continuing this design.

## Vision

The current codebase is a financial-document extractor with a hardcoded ontology
(`financial_documents.ttl`). The goal is to make it fully general:

- **`docgraph init`** seeds only a meta-ontology — no domain classes.
- **`docgraph add <file>`** — the LLM figures out what kind of document it is and
  builds the knowledge graph accordingly.
- **`docgraph remove <file>`** — cascades: removes concepts the document defined,
  and degrades any individuals previously classified under those concepts to bare
  `iso15926:ArrangedIndividual` (unclassified, but not lost).

The result after adding three documents — a German invoice, an EU standard
defining Invoice, and a meta-document classifying types of standards — should be
a graph with:
- a class `:Invoice` rooted under `iso15926:ArrangedIndividual` and a
  matching `ClassOfInformationObject` instance, defined in the EU
  standard's named graph
- an individual for the invoice itself, typed as `:Invoice` in its own named
  graph
- meta-classification triples from the third document in yet another named
  graph

Removing the EU standard cascades: the `:Invoice` class definition disappears,
and the individual's `rdf:type :Invoice` triple is rewritten to
`rdf:type iso15926:ArrangedIndividual` (unclassified, but not lost).

---

## Pipeline shape: format-specific extraction + uniform analyzer

Every ingest is one shape regardless of input format. Format-specific parsers
do the front half — turn the source into candidate triples in the source's own
vocabulary. A uniform **analyzer** does the back half — detect what was
defined, normalize non-canonical idioms, anchor classes to Part 2, emit the
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
… future formats ─► │  …                                  ─┼──┼─►  Phase 3: anchor declared classes to ISO 15926 Part 2     │
                    │                                      │  │                                                            │
                    └──────────────────────────────────────┘  │  Phase 4: emit named graph + register in sources.ttl       │
                                                              │                                                            │
                                                              └────────────────────────────────────────────────────────────┘
```

Two consequences:

- **TTL doesn't "skip" extraction** — it has *cheap* extraction (parsing) instead of
  expensive (PDF → MD → vision LLM). Everything from Phase 1 rightward is identical.
- **Convergence is approximate, not bit-identical.** A PDF describing schema.org and the
  schema.org TTL itself ingest to *similar* graphs, not identical: the PDF path is lossier
  and has to resolve URIs ("an Invoice…" → which Invoice?) the TTL gets for free. Useful
  as a test target — "the two should overlap on ≥ N% of classes/properties" — not a
  guaranteed equality.

### What can a document define?

After extraction, Phase 1 of the analyzer asks three independent yes/no questions and
records the answers as `dg:defines` triples:

| Question | Stored as | Triggered by |
|---|---|---|
| Defines classes? | `<source> dg:defines dg:Classes` | `?x a owl:Class`, `rdfs:Class`, `skos:Concept`, … |
| Defines properties? | `<source> dg:defines dg:Properties` | `?x a owl:ObjectProperty`, `owl:DatatypeProperty`, `rdf:Property`, … |
| Defines individuals? | `<source> dg:defines dg:Individuals` | `?x a <some-class-not-in-the-meta-vocabulary>` |

Any combination is valid. An ontology TTL with named individuals → all three. A receipt
PDF → `dg:Individuals` only. A standards PDF defining what an Invoice is → `dg:Classes`
and `dg:Properties` (and possibly some illustrative individuals).

This **replaces** the earlier binary `dg:detectedRole = DefinesTypes | AssertsInstances`
— the binary was too narrow.

### Subject (Q1) and form (Q2) still apply, separately

The earlier classification questions are orthogonal to the structural "what does it
define" axis. A document that *defines* `schema:Invoice` is not the same as a document
that *is* an instance of `schema:Invoice`. Q1/Q2 answer the latter; the "defines" axis
answers the former. Both can apply to the same source. See "Classification" below for
Q1/Q2 details.

---

## Meta-ontology — ISO 15926 Part 2

The meta-ontology **is** ISO 15926-2:2003 (the data model of the original standard,
shipped as the POSC Caesar OWL rendering). All meta-classes use Part 2 entity names
and URIs. Custom classes must not be invented where a Part 2 class already covers the
concept.

### Why Part 2

Part 2 reifies every relationship as a class — `RepresentationOfThing`,
`CompositionOfIndividual`, `Classification`, `Specialization`, etc. are
all `subClassOf #Relationship`, and instances of those classes *are* the
relationship-tuples. This is verbose, but it's the right shape for what
docgraph does: ingest documents that may assert temporal, sourced, or
contextual classifications and relationships. A document saying *"Pump
P-101 was classified as 'centrifugal' by ACME Engineering on 2020-03-15"*
is a fact whose temporal extent and authority are part of the assertion —
not just a flat `rdf:type`. Part 14 collapses these into atemporal OWL,
which is fine for static taxonomies but loses information when the source
is itself making time-bounded or attributed claims. Part 2 keeps it.

### Official OWL representation

The Part 2 ontology ships locally as RDF/XML at:

```
docs/ISO-15926-2_2003.rdf              ← class + property definitions
docs/ISO-15926-2_2003_annotations.rdf  ← rdfs:label / rdfs:comment annotations
```

Plus a verbatim extract of the standard's text for the information-object
sections at `docs/ISO-15926-2_2003_information_objects.md` (used as in-context
reference; not loaded into the graph).

Base namespace (the `iso15926:` prefix):

```
http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#
```

Hash-separated IRIs (e.g. `iso15926:ArrangedIndividual` =
`http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#ArrangedIndividual`).

`meta.ttl` should `owl:imports` the local RDF/XML files so the full Part 2 hierarchy is
available in the combined graph without any network fetch. The `iso15926:` prefix maps
to the namespace above (existing code already does this — see
`src/classify_part2/ns.py`).

### When to reify, when to use plain RDFS — the docgraph rule

Part 2 reifies *everything*. Docgraph deliberately doesn't — strict reification of
typing produces ~25 triples per evidenced quote and offers no value when the typing is
just structural scaffolding. The rule:

| Triple shape | Use plain `rdf:type` / `rdfs:subClassOf` | Use reified `Classification` / `Specialization` |
|---|---|---|
| Static structural typing — *"this file is a PDF", "Invoice is a kind of FinancialDocument"* | ✅ | ✗ |
| Sourced/temporal classification — *"the EU standard classifies this as a Type-B widget, valid from 2024"*, *"document X classified Y as Z"* | ✗ | ✅ |
| Vocabulary scaffolding — *"`fin:FinancialDocument rdfs:subClassOf iso15926:ArrangedIndividual`"* | ✅ | ✗ |
| Document-asserted subclass relations with attribution — *"per RFC 9999, A is a subkind of B"* | ✗ | ✅ |

Decision criterion: does the typing/subclass relation carry information that **should
not be true at all times** or that has a **specific source/authority** worth preserving
beyond the named-graph level? If yes, reify; otherwise plain RDFS.

The named-graph-per-source layer already gives source attribution at the *triple-set*
level for free. Reifying Classification/Specialization is only worth the cost when the
classification or subclass relation is itself *the fact being asserted* by the source
(rather than just the source's own structural use of the vocabulary).

### When to reify — actual relationships (always reified)

For genuine semantic relationships between individuals, Part 2's reification is the
only correct shape — there's no `rdf:type`-shortcut alternative. These are always
reified:

- `iso15926:RepresentationOfThing` (and subtypes `Description`, `Identification`,
  `Definition`) — *"this sign represents that thing"*
- `iso15926:CompositionOfIndividual` — *"this is part of that"*
- `iso15926:ResponsibilityForRepresentation` — *"this party is responsible for that
  representation"*
- `iso15926:UsageOfRepresentation` — *"this party uses that representation"*

Each is a class whose instances are reified relationship-tuples carrying their two
endpoints as named slots. See `docs/ISO-15926-2_2003_information_objects.md` for the
authoritative entity definitions.

### Top-level Part 2 hierarchy relevant to docgraph

```
iso15926:Thing
  iso15926:AbstractObject              quantities, measures, classes-as-individuals
  iso15926:PossibleIndividual          everything that can have spatial/temporal extent
    iso15926:ArrangedIndividual        ← documents, signs, organisations, parts
    iso15926:Event                      a 0-D temporal point
  iso15926:Relationship                 reified relationships (Classification,
                                        CompositionOfIndividual, RepresentationOfThing,
                                        Specialization, …)
```

Two key things this captures that Part 14 didn't:

1. **`ArrangedIndividual`** is the workhorse — documents, file renderings, quotes,
   labels, signs, even most "things" extracted from documents are arranged individuals.
   They become more specific via classification (`a iso15926:ArrangedIndividual,
   ext:Invoice`) without needing dozens of new top-level types.
2. **`relationship`** is the reified-relation root. Every Composition, Classification,
   etc. carries its slot-bearing tuples as instances of these subclasses.

Beyond these, Part 2 has a richer cast of meta-classes (`ClassOfArrangedIndividual`,
`ClassOfInformationObject`, `ClassOfInformationRepresentation`,
`class_of_ClassOfInformationRepresentation`, etc.) used to type the *kinds* of thing
(see "Information objects" below).

### What Part 2 does *not* model — the `dg:` extension namespace

Even Part 2 omits a few things docgraph needs at the *ingestion* layer:

| Concept | Part 2 status | docgraph approach |
|---|---|---|
| Modality (MUST / SHOULD / MAY / MUST NOT) | Not modelled | `dg:Modality` class with four instances |
| Source / ingestion bookkeeping | Out of scope | named graphs + `dg:` registry |
| Unresolved-stub status | Out of scope | `dg:status dg:Unresolved` |
| Evidence-quote payload (`dg:text`, `dg:locator`) | No literal-attached payload primitive | `dg:` literal annotations on quote individuals |
| Structural roles for files, documents, chapters, quotes | Only the metaclasses `ClassOfInformationObject` / `ClassOfInformationRepresentation` exist; no instance-level `Document` / `Quote` / `File` classes | `dg:Document`, `dg:Chapter`, `dg:Quote`, `dg:File`, `dg:PdfFile`, `dg:MarkdownFile` (see next subsection) |

The `dg:` namespace (`http://example.org/docgraph/meta#`) is reserved for these
docgraph-specific additions. Every structural class must come from `iso15926:` if Part 2
covers it.

### Docgraph structural classes (`dg:File` / `dg:Document` / `dg:Chapter` / `dg:Quote`)

Part 2 has only metaclasses for information objects (`ClassOfInformationObject`,
`ClassOfInformationRepresentation`) — no instance-level `Document` or `Quote` class.
`ArrangedIndividual` (the only suitable instance-level superclass) is too abstract: it
just means *"this thing is composed of parts"* and says nothing about being information.
Without docgraph-owned classes, every quote and document would be typed as a generic
`ArrangedIndividual` with no info-object semantics.

Docgraph defines its own classes for the four structural roles, baked into `meta.ttl`.
Each is **OWL-2 punned** — declared as both an `owl:Class` (so its instances are
individuals like a specific document) *and* an instance of the right Part 2 metaclass
(so the "this is information" semantics are captured ontologically):

```turtle
@prefix dg:       <http://example.org/docgraph/meta#> .
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .

# ── Information objects (abstract content) ────────────────────────
dg:Document  a owl:Class, iso15926:ClassOfInformationObject ;
             rdfs:subClassOf iso15926:ArrangedIndividual ;
             rdfs:label "Document (abstract content)" .

dg:Chapter   a owl:Class, iso15926:ClassOfInformationObject ;
             rdfs:subClassOf iso15926:ArrangedIndividual ;
             rdfs:label "Document chapter / section" .

dg:Quote     a owl:Class, iso15926:ClassOfInformationObject ;
             rdfs:subClassOf iso15926:ArrangedIndividual ;
             rdfs:label "Evidence quote" .

# ── Renderings (bytes-on-disk, classified by encoding/format) ─────
dg:File         a owl:Class, iso15926:ClassOfInformationRepresentation ;
                rdfs:subClassOf iso15926:ArrangedIndividual ;
                rdfs:label "File rendering" .

dg:PdfFile      a owl:Class, iso15926:ClassOfInformationRepresentation ;
                rdfs:subClassOf dg:File ;
                rdfs:label "PDF file" .

dg:MarkdownFile a owl:Class, iso15926:ClassOfInformationRepresentation ;
                rdfs:subClassOf dg:File ;
                rdfs:label "Markdown file" .
```

Why split `ClassOfInformationObject` (Document/Chapter/Quote) from
`ClassOfInformationRepresentation` (File/PdfFile/MarkdownFile)? Per the standard
(§5.2.8.9, §5.2.17.4): an *information object* is the abstract content; an
*information representation* is the encoding/pattern. A document has content; a file has
format. The same document can have multiple file renderings (PDF + Markdown) — that's
the whole point of the file ↔ document split in the chain below.

The `dg:` instance-level individuals (`ext:doc-acme-q3`, `ext:file-acme-pdf`,
`ext:quote-3f7a9c`) are typed *only* with the docgraph class — `rdf:type dg:Document`
etc. They don't carry an explicit `rdf:type iso15926:ArrangedIndividual` triple; that
follows transitively from `dg:Document rdfs:subClassOf iso15926:ArrangedIndividual` and
is materialised by any reasoner.

### Built-in modality individuals (RFC 2119 as docgraph individuals)

Baked into `meta.ttl`. They represent the normative modality vocabulary from RFC 2119 /
ISO drafting directives:

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

## Information objects: file → document → chapter → quote chain

Every PDF ingest produces a chain of `ArrangedIndividual`s linked by reified
relationships. The chain is the source of truth for cascade-delete, evidence
deduplication, and per-quote provenance.

```
File rendering ──[RepresentationOfThing]──► Document
                                                  │
                                  [CompositionOfIndividual]
                                                  ▼
                                              Chapter (optional)
                                                  │
                                  [CompositionOfIndividual]
                                                  ▼
                                              Quote ──[description]──► Individual / Class
                                                                       (the thing the quote is about)
```

### Concrete shape (turtle)

Uses the `dg:` structural classes defined above (`dg:PdfFile`, `dg:Document`,
`dg:Chapter`, `dg:Quote`) plus a domain-specific subtype of `dg:Document` for the
report kind:

```turtle
# Domain subtype of dg:Document (lives in the financial-domain graph, not meta.ttl)
dom:QuarterlyReport  rdfs:subClassOf dg:Document ;
                     rdfs:label "Quarterly report" .

# Level 1: File (the bytes/rendering)
ext:file-acme-pdf    a dg:PdfFile .              # transitively ArrangedIndividual

# Level 2: Document (what the file represents)
ext:doc-acme-q3      a dom:QuarterlyReport .     # transitively dg:Document, ArrangedIndividual

# File ↔ Document — reified RepresentationOfThing
ext:rep-file-doc     a iso15926:RepresentationOfThing ;
                     iso15926:hasSign        ext:file-acme-pdf ;
                     iso15926:hasRepresented ext:doc-acme-q3 .

# Level 3: Chapter (optional, when markdown extractor produced headings)
ext:ch1-revenue      a dg:Chapter ;
                     rdfs:label "Chapter 1: Revenue" .
ext:comp-doc-ch1     a iso15926:CompositionOfIndividual ;
                     iso15926:hasWhole ext:doc-acme-q3 ;
                     iso15926:hasPart  ext:ch1-revenue .

# Level 4: Quote (the evidence)
ext:quote-3f7a9c     a dg:Quote ;
                     dg:text     "Q3 revenue was €1.2B, up 8% YoY." ;
                     dg:locator  "p.12" .
ext:comp-ch1-quote   a iso15926:CompositionOfIndividual ;
                     iso15926:hasWhole ext:ch1-revenue ;
                     iso15926:hasPart  ext:quote-3f7a9c .

# Level 5: Quote → referenced individual — reified Description
ext:desc-quote-rev   a iso15926:Description ;    # SUBTYPE OF RepresentationOfThing
                     iso15926:hasSign        ext:quote-3f7a9c ;
                     iso15926:hasRepresented ext:ind-acme-q3-revenue .
```

The PDF→Markdown derivation (when the file is a PDF) introduces a sibling rendering
under the same document, plus a PROV-O activity recording the conversion process — see
"PDF → Markdown derivation" subsection below.

### Design rules

1. **Use `Description` for evidence-quote relationships by default.** It's the
   `RepresentationOfThing` subtype meaning *"this sign describes that thing"*. Switch
   to `Identification` only when the quote is genuinely just a label/identifier (e.g.
   a tag number); use `Definition` only when the represented thing is a class (Part 2
   restricts `Definition` to classes per §5.2.16.1).

2. **Chapters are optional.** Insert the chapter level only when the parser provided a
   heading hierarchy (PDF→markdown does, raw text doesn't). Otherwise quote
   `CompositionOfIndividual.hasWhole` points directly at the document.

3. **Quote URI = content hash** (e.g. first 10 hex chars of SHA-1 of the quote text).
   This gives free deduplication: the same sentence cited from N entities collapses to
   one quote node, with N `Description` relationships attached.

4. **Each quote can have multiple descriptions.** A single quote that mentions two
   entities produces two `Description` instances, both with the same `sign` (the quote)
   but different `represented` slots.

5. **The file ↔ document split is conformant but optional.** Strict Part 2 wants the
   reified `RepresentationOfThing` between bytes-on-disk and the document concept.
   Pragmatically, single-rendering documents can collapse the two into one node; the
   split is worth keeping when you may have multiple renderings (PDF + Word) of the
   same logical document.

6. **Each quote is in the source's named graph.** Cascade-delete drops the named graph,
   which drops the quote individuals and their composition / description tuples.
   Referenced individuals (`ext:ind-acme-q3-revenue`) live in the extraction graph and
   may survive (they get repaired per the cascade-delete rules below).

### Cost

Per evidenced quote: ~5 nodes / ~13 triples. Structural class nodes (`dg:PdfFile`,
`dg:Document`, `dg:Quote`, etc.) live in `meta.ttl` and aren't paid per-ingest. Domain
subtypes (`dom:QuarterlyReport`, etc.) live in the defining document's graph and are
paid once per ontology, not per instance. The deliberate trade vs. the old
`dg:evidence "..."` literal (1 triple, opaque) — picked because we want
quote-deduplication, multi-target referencing, page/locator metadata, and a clean
Part 2-shaped provenance graph that external tools can consume.

### PDF → Markdown derivation

When the source is a PDF, the markdown produced by the vision LLM is a *second
rendering* of the same document. Both renderings are siblings — neither is the
"canonical" one — and both link to the same `dg:Document` instance via separate
`RepresentationOfThing` reifications. The conversion is recorded with PROV-O.

```turtle
# Two renderings of the same document
ext:file-acme-pdf  a dg:PdfFile, prov:Entity ;
                   dg:filePath "..." ; dg:fileHash "..." ; dg:fileSize 123456 .
ext:file-acme-md   a dg:MarkdownFile, prov:Entity ;
                   dg:filePath "..." ; dg:fileHash "..." ;
                   prov:wasDerivedFrom ext:file-acme-pdf .

# Both represent the same document
ext:rep-pdf-doc    a iso15926:RepresentationOfThing ;
                   iso15926:hasSign        ext:file-acme-pdf ;
                   iso15926:hasRepresented ext:doc-acme-q3 .
ext:rep-md-doc     a iso15926:RepresentationOfThing ;
                   iso15926:hasSign        ext:file-acme-md ;
                   iso15926:hasRepresented ext:doc-acme-q3 .

# The conversion process (PROV-O — separate from Part 2 ontological structure)
ext:conv-pdf-md    a prov:Activity ;
                   rdfs:label             "PDF to Markdown conversion" ;
                   prov:used              ext:file-acme-pdf ;
                   prov:generated         ext:file-acme-md ;
                   prov:wasAssociatedWith <agent/claude-vision> ;
                   prov:startedAtTime     "2026-04-15T12:34:56Z"^^xsd:dateTime .
```

Two distinct concerns, two layers:

- **Part 2 / `dg:`** captures the *ontological* structure: what each artefact is
  (PdfFile, MarkdownFile) and how it relates to the document (both are
  RepresentationOfThing-linked sign individuals).
- **PROV-O** captures the *process* that produced one from the other (Activity, used,
  generated, agent, timing).

Part 2 has `ClassOfRepresentationTranslation` (§5.2.17.6) for relating two
representation classes, but it's class-level and clunky for instance-level "this MD
file was produced from this PDF". PROV-O is purpose-built for derivation provenance —
keep using it (the existing code already does, see `src/ingest_pdf.py:120-160`).

The chapter and quote individuals (Levels 3 and 4 of the chain above) are extracted
from the *markdown* rendering — but they hang off the `dg:Document` instance, not off
the markdown file. That way they survive a PDF→Markdown re-conversion: the file
individuals can be replaced (with new derivation Activity entries) while the document
and its quotes stay put.

---

## Provenance: named graphs + source-content reification

The project uses a two-layer provenance model:

1. **Named graphs** carry *source-level* provenance. Every triple lives in exactly one
   named graph. The graph URI *is* the source identifier. No per-triple `dg:definedBy`.
2. **Part 2 reification** (`Classification`, `Specialization`,
   `RepresentationOfThing`, `CompositionOfIndividual`, …) is used inside a graph
   when the *content* of the source asserts a fact whose temporal extent, authority,
   or context is part of the assertion (per the rule above).

The two layers are complementary: named graphs answer *"who wrote this triple set"*,
reification answers *"who/when/by-what-authority does this specific fact hold"*.

### Permanent backbone — `meta.ttl`

`meta.ttl` is the structural scaffolding written once by `init` and never overwritten.
It loads Part 2 and declares the docgraph-specific extensions:

```turtle
# meta.ttl — permanent scaffolding
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix dg:       <http://example.org/docgraph/meta#> .
@prefix owl:      <http://www.w3.org/2002/07/owl#> .

<http://example.org/docgraph/meta>  a owl:Ontology ;
    owl:imports <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003> .

# ── Modality ──────────────────────────────────────────────────────
dg:Modality   a owl:Class .
dg:Mandatory  a dg:Modality .
dg:Preferred  a dg:Modality .
dg:Optional   a dg:Modality .
dg:Prohibited a dg:Modality .
dg:modality   a owl:ObjectProperty ; rdfs:range dg:Modality .

# ── Structural classes for the file/document/chapter/quote chain ──
dg:Document  a owl:Class, iso15926:ClassOfInformationObject ;
             rdfs:subClassOf iso15926:ArrangedIndividual .
dg:Chapter   a owl:Class, iso15926:ClassOfInformationObject ;
             rdfs:subClassOf iso15926:ArrangedIndividual .
dg:Quote     a owl:Class, iso15926:ClassOfInformationObject ;
             rdfs:subClassOf iso15926:ArrangedIndividual .

dg:File         a owl:Class, iso15926:ClassOfInformationRepresentation ;
                rdfs:subClassOf iso15926:ArrangedIndividual .
dg:PdfFile      a owl:Class, iso15926:ClassOfInformationRepresentation ;
                rdfs:subClassOf dg:File .
dg:MarkdownFile a owl:Class, iso15926:ClassOfInformationRepresentation ;
                rdfs:subClassOf dg:File .

# ── Quote payload ─────────────────────────────────────────────────
dg:text       a owl:DatatypeProperty ;
              rdfs:domain dg:Quote ;
              rdfs:range  xsd:string .
dg:locator    a owl:DatatypeProperty ;
              rdfs:domain dg:Quote ;
              rdfs:range  xsd:string .
```

### Document-sourced assertions

When a document asserts that "Invoice is a subtype of FinancialDocument" or that "this
invoice IS an Invoice", these are plain OWL triples written into the document's named
graph (per the typing-vs-reification rule — these are static structural assertions):

```turtle
# graphs/eu-standard.ttl — named graph for the EU standard
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix dom:      <http://example.org/docgraph/domain/> .

dom:Invoice  a owl:Class ;
    rdfs:subClassOf iso15926:ArrangedIndividual ;
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

When a source needs to assert a *temporal* or *authority-bearing* classification,
reification is used inside the graph (the `mint_classification` helper in
`src/classify_part2/reify.py` already implements this).

### Cascade delete

`docgraph remove eu-standard.pdf`:

1. Look up the graph file in `sources.ttl` → `graphs/eu-standard.ttl`.
2. Parse it; collect every class and property URI defined there (subjects with
   `rdf:type owl:Class`, `owl:ObjectProperty`, or `owl:DatatypeProperty`).
3. Show the user what will be removed (concepts + dependent individuals).
4. On confirm: delete the graph file and remove its registry entry.
5. Scan the remaining named graphs for triples whose predicate or `rdf:type` referenced
   a now-undefined concept:
   - `<x> rdf:type <removed-class>` → rewrite to
     `rdf:type iso15926:ArrangedIndividual` (if the removed class was a subclass of
     `ArrangedIndividual`) or remove the triple otherwise.
   - `<x> <removed-property> _` → remove the triple.
   - Reified `Classification` / `Specialization` instances whose `hasClassifier` /
     `hasSubclass` / etc. pointed at the removed concept → drop the reification node
     entirely.

The meta backbone (`meta.ttl`) is never touched.

### TTL ingest is one parser among several

A `.ttl` source goes through the same pipeline as any other input: parse → analyzer
(Phase 1–4) → named graph. The TTL parser is just *cheaper* than the PDF parser (no
vision LLM step). For pure-OWL TTLs like `data/financial_documents.ttl`, Phase 2
(normalization) and Phase 3 (Part 2 anchoring) are no-ops — the source already uses
canonical predicates and roots under `iso15926:`. For schema.org or SKOS, Phase 2
rewrites idioms via lift rules (see "Analyzer pipeline" below).

The ingest stamps the registry with `dg:addedAt` and one or more `dg:defines` values
determined by Phase 1 (Classes, Properties, Individuals).

---

## DEFINE vs REFERENCE — ownership

For every concept the system encounters in a document, the LLM (or the TTL ingester)
must decide:

| Relationship | Meaning | Lifecycle |
|---|---|---|
| Concept defined in this document's graph | This document is the normative source | Remove doc → drop the graph → concept gone |
| Concept referenced but defined elsewhere | This document uses, doesn't own | Remove doc → no effect on the concept |

With named-graph provenance, ownership is *positional*: a concept is defined by
whichever graph contains its declaration triple (`a owl:Class` plus `rdfs:subClassOf …`).
A referencing document just uses the URI without redeclaring it.

When ambiguity arises (the same URI appears with `a owl:Class` in two graphs), it's a
merge conflict — see open questions below.

### Unresolved concepts

If a document references a concept that has no defining document yet, we can't simply
omit it — we lose the reference. Instead, the ingester mints a **stub** in a dedicated
`graphs/_unresolved.ttl` graph:

```turtle
# graphs/_unresolved.ttl
dom:Invoice  a iso15926:ArrangedIndividual ;
    dg:status         dg:Unresolved ;
    dg:firstSeenIn    <source/german-invoice.pdf> .
```

A stub is typed as plain `iso15926:ArrangedIndividual` (no subclass relationship yet)
and flagged `dg:Unresolved`. When a defining document is later added, the loader:

1. Detects that the new graph defines `dom:Invoice` (i.e., contains
   `dom:Invoice a owl:Class ; rdfs:subClassOf …`).
2. Removes the stub triples from `_unresolved.ttl`.
3. Optionally rewrites individuals in other graphs that were typed as
   `iso15926:ArrangedIndividual` but referenced through `dom:Invoice` to use the
   now-defined class.

This makes the **order of ingestion irrelevant** — documents can be added in any order
and the graph heals itself.

`dg:status`, `dg:Unresolved`, and `dg:firstSeenIn` are docgraph-specific (no Part 2
equivalent for ingestion bookkeeping).

---

## Modality and SHACL derivation

Modality is extracted directly from normative text and stored as triples on the
property declaration, in the defining document's named graph:

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

Modality is a `dg:`-namespace simplification, not a reified Part 2 chain — modality
is a structural property of the property *definition*, not an event-with-extent, so
plain `rdf:type` / `dg:modality` is the right shape.

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

Removing the defining document drops its named graph → modality triples vanish →
derived shapes change automatically.

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

Regardless of input format, `graphs/<slug>.ttl` is a real file written by the analyzer
(Phase 4) — never a symlink to the source. The analyzer's output is the *normalized
view* (Phase 2 rewrites + Phase 3 anchors + canonical triples the source already had),
and that view is rarely byte-identical to the source. Storing it as a real file lets
cascade-delete drop it cleanly without touching the user's original input.

The original TTL/PDF source stays where the user put it; the registry references it by
path, but the graph is ours.

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

### Cascade delete

`docgraph remove <file>`:
1. Look up the graph file in `sources.ttl`.
2. Parse it; collect every class and property URI it declares.
3. Show the user what will be removed (concepts + dependents).
4. On confirm: delete the graph file, remove from `sources.ttl`.
5. Scan all other graph files for triples that reference the removed URIs and repair
   them (rewrite type to `iso15926:ArrangedIndividual` or drop the triple, per the
   rules above).

---

## Analyzer pipeline (Phase 1–4)

The analyzer is the format-agnostic back half of every ingest. It runs once per source,
after the format-specific parser has produced candidate triples in the source's own
vocabulary.

### Phase 1 — detect what the source defines

Walk the candidate triples. Answer the three "defines" questions (Classes, Properties,
Individuals) by structural inspection — no LLM call needed:

```
declares ?x a owl:Class | rdfs:Class | skos:Concept …  →  dg:Classes
declares ?x a owl:ObjectProperty | DatatypeProperty | rdf:Property …  →  dg:Properties
declares ?x a <C>, where <C> is not in the meta-vocabulary  →  dg:Individuals
```

Emit `<source> dg:defines …` triples. This drives which subsequent phases need to run:
a pure instance document skips Phase 2/3 (no classes to normalize or anchor); a pure
ontology skips downstream individual-extraction.

### Phase 2 — normalize non-canonical idioms

For every declared class and property, check whether its **structural slots** are
filled with canonical predicates:

- A property declared without `rdfs:domain`/`rdfs:range` but *with*
  `schema:domainIncludes` or similar → idiom needs a lift rule.
- A class declared without `rdfs:subClassOf` parent but *with* `skos:broader` → same.
- A `rdf:Property` declaration with no `owl:DatatypeProperty`/`ObjectProperty` typing,
  where the range determines which → same.

Pure-OWL inputs have all slots filled canonically and Phase 2 is a no-op. The detection
is automatic — the user doesn't declare "this needs normalization", the analyzer finds
it by inspection.

For each idiom predicate that triggered the signal, the analyzer looks up a **lift rule**
(a SPARQL CONSTRUCT) in two locations:

```
data/normalization/         ← pre-seeded rules shipped with docgraph
  schemaorg.rq              ← schema:domainIncludes/rangeIncludes/property typing
  skos-as-taxonomy.rq       ← skos:broader/narrower → rdfs:subClassOf
.docgraph/cache/lifts/      ← runtime-discovered rules (LLM-generated, user-approved)
  <predicate-slug>.rq
```

Both locations are equal-status. The loader unions all matching rules. There is no
"deterministic vs LLM" split in code — pre-seeded entries are just LLM-work-already-
done-at-build-time, in the same on-disk format the runtime cache uses. Users can
override or delete pre-seeded entries.

If a non-canonical idiom has no rule in either location, Phase 2 prompts the LLM with
the predicate URI and its `rdfs:label`/`comment` from the source, and asks for a
CONSTRUCT-shaped rewrite (or "pass through" if it was already canonical and Phase 2's
heuristic was wrong). Output is shown to the user for approval, then cached in
`.docgraph/cache/lifts/`. Cache key is the predicate URI — predicate semantics are
vocabulary-stable, so the same predicate seen in the next ingest reuses the rule.

### Phase 3 — anchor declared classes to Part 2

For every class declared in the (now-normalized) source, walk `rdfs:subClassOf*` upward.
If it terminates at any `iso15926:` class → no anchor needed. If it doesn't, send to
the LLM with a curated upper-level Part 2 catalogue (~15 top-level classes:
`ArrangedIndividual`, `PhysicalObject`, `Organism`, `Organization`, `Event`,
`Activity`, `Role`, `Quality`, `Function`, `Disposition`, `ClassOfInformationObject`,
`ClassOfInformationRepresentation`, etc.) and get back one of:

- `<class> rdfs:subClassOf iso15926:<X>` — the closest-fit Part 2 superclass.
- `<class> dg:noPart2Anchor true` — class has no Part 2 home (e.g.,
  `schema:PaymentMethod`, an intangible classifier); leave it unrooted.

User reviews. Cached per class URI in `.docgraph/cache/anchors/`. Anchoring permits
"no anchor" rather than forcing every class up to `iso15926:Thing` — otherwise the
hierarchy fills with noise.

### Phase 4 — emit named graph

Write the normalized graph (Phase 2 rewrites + Phase 3 anchors + everything the source
already declared canonically) to `graphs/<slug>.ttl` and register in `sources.ttl`.
Cascade-delete drops the file and the registry entry.

### Caching summary

Two long-lived caches survive source removal — they're vocabulary-level facts, not
document-level:

```
.docgraph/cache/lifts/<predicate-slug>.rq    ← per-predicate lift rule
.docgraph/cache/anchors/<class-slug>.ttl     ← per-class Part 2 anchor
```

Same shape as the PDF→Markdown cache (cache the expensive LLM work so it doesn't
re-run), different key. `docgraph forget-rule <uri>` evicts an entry that was approved
in error.

### Bootstrap

`data/financial_documents.ttl` is the canonical Phase-2/3 no-op test: ingesting it
should produce a normalized graph byte-equivalent to the source modulo blank-node
renaming. If it doesn't, the analyzer is over-rewriting.

---

## Classification — two questions (Q1 + Q2)

Classification of an ingested document splits into two independent questions asked in
order. They have different scopes, different candidate sets, and different cost
profiles.

These are orthogonal to the structural axis introduced in "Pipeline shape" — *what does
this document define?* (Classes / Properties / Individuals). Q1/Q2 ask about the
document's subject and form. The structural axis runs in the analyzer (Phase 1) by
inspecting triples; Q1/Q2 are LLM-driven semantic calls. Both result sets land on the
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
- **Doubles as the uncovered diagnostic**: if Q1 returns only the most generic subjects
  (`ArrangedIndividual` and nothing more specific) with low confidence, the document
  is outside the upper ontology's resolution. Replaces the earlier
  `dg:typeNearestSimilarity < 0.3` geometric heuristic with a semantically grounded
  one.

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
    dg:typeCoverage       0.67 ;            # filled-direct-props / total (if Q2 ran)
    dg:typeNearestSimilarity 0.27 ;         # best Q2 cosine score (if Q2 ran)
    dg:isAbout            iso15926:Activity, iso15926:Person .  # Q1 result
```

Reading them together gives the diagnostics the user wants:
- High `subjectConfidence` + Q2 didn't run → "we know what it's about; you haven't
  loaded a form ontology yet."
- High `subjectConfidence` + low `typeNearestSimilarity` → "we know the general topic;
  no loaded form fits — the document is outside this project's domain coverage."
- High `subjectConfidence` + high `typeConfidence` + low `typeCoverage` → "right type,
  but document is sparse — many of the type's declared properties weren't in the
  document."

---

## Extraction pipeline (full sequence)

The unified pipeline that "Pipeline shape" introduces, with concrete steps:

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
    ├─ 3. Analyzer Phase 1 — what does this source define?
    │     Structural inspection of candidate triples. Emit
    │     <source> dg:defines dg:Classes/Properties/Individuals.
    │
    ├─ 4. Analyzer Phase 2 — normalize non-canonical idioms.
    │     For each declared class/property with empty structural slots,
    │     look up lift rules in data/normalization/ + cache/lifts/, prompt
    │     LLM if missing, apply. Pure-OWL inputs are no-ops.
    │
    ├─ 5. Analyzer Phase 3 — anchor declared classes to Part 2.
    │     For each class without an iso15926: ancestor, LLM picks closest fit
    │     from the curated upper-level Part 2 catalogue (or "no anchor").
    │     Cached per class URI. Skipped if Phase 1 found no Classes.
    │
    ├─ 6. Q1 — Subject identification (LLM, semantic).
    │     Candidates: ~15 curated upper-level Part 2 classes, sent in full.
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
    │     named graph. Each extracted entity carries one or more reified
    │     description tuples linking the supporting quote(s) to it.
    │     Coverage signal: filled-direct / total-direct.
    │
    └─ 9. Analyzer Phase 4 — emit named graph and register in sources.ttl.
```

Steps 3–5 are the analyzer's class/property work; steps 6–8 are subject/form
classification and per-document property extraction. They share the same named graph
(`<ext/<slug>>` for the extraction graph; `graphs/<slug>.ttl` for the normalized source
view).

The extraction graph is described as a `prov:Entity` in the default graph and
generated by all the LLM activities above (Phase 2 normalization, Phase 3 anchoring,
Q1, Q2, property extraction). See "Provenance" above for the cascade story.

---

## What `docgraph init` produces

After init, `.docgraph/` contains only:

```
meta.ttl       ← imports ISO 15926 Part 2 + declares dg: extensions
                 (dg:Document, dg:Chapter, dg:Quote, dg:File, dg:PdfFile, dg:MarkdownFile,
                  dg:Modality, dg:Mandatory/Preferred/Optional/Prohibited, dg:modality,
                  dg:status, dg:Unresolved, dg:IngestionRecord,
                  dg:defines, dg:Classes/Properties/Individuals,
                  dg:noPart2Anchor, dg:text, dg:locator, etc.)
sources.ttl    ← empty registry
graphs/        ← contains only an empty _unresolved.ttl
cache/
  pdfmd/       ← PDF → Markdown cache (per-document, key = doc hash)
  lifts/       ← LLM-discovered lift rules (per-predicate, key = predicate URI)
  anchors/     ← LLM-discovered Part 2 anchors (per-class, key = class URI)
```

No `financial_documents.ttl`. No domain classes. The graph is empty except for
structure. When the combined graph is loaded, `meta.ttl`'s `owl:imports` brings in
Part 2 and the full hierarchy is available for classification.

### Pre-seeded normalization rules (shipped with docgraph, not in `.docgraph/`)

The repo ships a small set of lift rules for common vocabularies under
`data/normalization/`:

```
data/normalization/
  schemaorg.rq        ← schema:domainIncludes/rangeIncludes, rdf:Property typing
  skos-as-taxonomy.rq ← skos:broader/narrower → rdfs:subClassOf
```

These are pre-seeded equivalents of `cache/lifts/` entries — same on-disk format, same
code path. The user pays no LLM cost for first-time ingest of schema.org or SKOS-shaped
vocabularies; everything else still flows through the LLM-discovered route.

---

## Future: triplestore migration

Current plan uses **rdflib `Dataset`** with TriG/N-Quads format for named graphs,
stored as files. This is readable, version-controllable, and testable on small corpora.

When scale requires it, the file layout maps 1-to-1 to a triplestore's named graphs
(Oxigraph, Apache Fuseki). Migration path: replace file I/O with SPARQL HTTP client,
keep the same graph URI scheme.

---

## Open questions / next decisions

1. **ISO 15926 Part 2 mapping** *(resolved 2026-05-02)*: Part 2 (POSC Caesar OWL
   rendering) is the meta-ontology. Key decisions:
   - Use `iso15926:` prefix for `http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#`.
   - Document instances are `iso15926:ArrangedIndividual`s, classified by an instance
     of `ClassOfInformationObject`.
   - Document types → OWL classes with `rdfs:subClassOf iso15926:ArrangedIndividual`.
   - Properties → `owl:ObjectProperty` / `owl:DatatypeProperty` with
     `rdfs:domain`/`range`.
   - Modality (Mandatory/Preferred/Optional/Prohibited) is docgraph-specific
     (`dg:Modality` enum) — Part 2 has no equivalent at the property-modality layer.
   - Source-level provenance is the named graph; per-fact temporal/authoritative
     provenance uses Part 2 reification (`Classification`, `Specialization`,
     `RepresentationOfThing`, etc.) per the rule above.
   - Evidence quotes are `ArrangedIndividual`s linked to their supporting
     `Description` and `CompositionOfIndividual` reifications.

2. **Prototype order**: TTL ingest first (proves meta-ontology structure, no LLM risk)
   or PDF role-detection first (proves the LLM pipeline)?

3. **`docgraph remove`**: Show diff of what will cascade before confirming?

4. **`docgraph status`**: Surface contents of `_unresolved.ttl` — "these concepts are
   referenced but have no defining document".

5. **Merge conflicts**: Two documents declare the same URI as `owl:Class` with
   different `rdfs:subClassOf` parents. Options: last-write-wins, explicit conflict
   node (`dg:ConflictingDefinition`), or require user resolution.

6. **Scope / temporal validity**: When a standard has a validity period or
   jurisdiction, attach it to the *named graph* (registry entry in `sources.ttl`), not
   to each triple. Confirm this is sufficient for the use cases on the table.

7. **Existing `financial_documents.ttl`**: ingest via the new analyzer pipeline. Should
   be a Phase-2/Phase-3 no-op (already canonical OWL, already roots under
   `iso15926:`). The bootstrap test for "the analyzer doesn't over-rewrite".

8. **LLM rule approval flow**: Phase 2 lift discovery and Phase 3 anchor discovery
   both want user review before caching. Bundle into one combined diff at end of
   ingest ("here's how I translated this source — accept / edit / abort") or two
   separate prompts? Probably one combined diff.

9. **Pre-seeded vs cached rule conflict**: if a user runs `docgraph add` on a
   schema.org TTL, gets the pre-seeded lift, later edits `cache/lifts/` to override,
   then a docgraph upgrade ships a new pre-seeded version — whose wins? Probably the
   cache (it's user-owned), with a `docgraph diagnose` command to surface the
   divergence.

10. **"No anchor" surface**: `dg:noPart2Anchor true` is queryable but noisy (every
    Part-2-foreign class carries the annotation). Alternative: silent (just leave
    class unrooted) and derive the "outside Part 2" set with a SPARQL query.
    Convenience-vs-cleanliness call.

11. **Quote-individual deduplication scope**: SHA-1 hash of quote text gives free
    cross-source dedup, but means a quote URI in graph A can be referenced by graph B's
    description tuples. Options: (a) keep dedup global — a quote with the same text
    is the same node regardless of source; (b) dedup per-source only — each source's
    graph re-mints its own quote URIs even for identical text. (a) is conceptually
    cleaner; (b) makes cascade-delete trivially local. Probably (a) with a registry
    of quote-graph-of-origin to enable repair on cascade.

---

## Current codebase reference

Key files for the Part 2 pipeline:

| File | Role |
|---|---|
| `src/classify_part2/` | Part 2 classification pipeline (14-prompt classifier) |
| `src/classify_part2/ns.py` | Namespace constants (`ISO15926`, `DG`, `EXT_NS_FOR`) |
| `src/classify_part2/reify.py` | `mint_classification`, `mint_class_of` helpers |
| `src/classify_part2/uri.py` | Deterministic URI minting (slugify + ext namespace) |
| `src/classify_part2/pipeline.py` | Top-level entry point for one source |
| `src/classify_part2/runner.py` | Per-prompt LLM driver |
| `src/classify_part2/convert/` | Per-prompt JSON-to-TTL converters |
| `docs/ISO-15926-2_2003.rdf` | Part 2 OWL rendering (POSC Caesar) |
| `docs/ISO-15926-2_2003_annotations.rdf` | Part 2 annotation overlay |
| `docs/ISO-15926-2_2003_information_objects.md` | Verbatim extract of the standard's information-object sections |
| `docs/classify_design.md` | High-level design + gating logic for the 14-prompt pipeline |
| `docs/classify_prompts/` | One markdown file per prompt body |
| `data/financial_documents.ttl` | Hardcoded domain ontology (to be replaced by the general pipeline) |
| `data/docgraph.ttl` | Project registry (to be redesigned around `sources.ttl`) |
| `data/shapes.ttl` | Hand-authored SHACL shapes (to be derived from modality triples) |
