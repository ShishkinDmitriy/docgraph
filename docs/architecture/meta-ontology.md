# Meta-ontology — ISO 15926 Part 2

The meta-ontology **is** ISO 15926-2:2003 (the data model of the original standard,
shipped as the POSC Caesar OWL rendering). All meta-classes use Part 2 entity names
and URIs. Custom classes must not be invented where a Part 2 class already covers the
concept.

## Why Part 2

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

## Official OWL representation

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

## When to reify, when to use plain RDFS — the docgraph rule

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

## When to reify — actual relationships (always reified)

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

## Top-level Part 2 hierarchy relevant to docgraph

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
(see [`information-objects.md`](information-objects.md)).

## What Part 2 does *not* model — the `dg:` extension namespace

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

A sibling namespace `tpl:` (`http://example.org/docgraph/template#`) carries the
template metamodel — `tpl:Template`, `tpl:slot`, `tpl:lifted`, `tpl:lowered`,
`tpl:Invocation`, etc. See [`templates.md`](templates.md) for the full vocabulary
and design.

## Docgraph structural classes (`dg:File` / `dg:Document` / `dg:Chapter` / `dg:Quote`)

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
the whole point of the file ↔ document split in [`information-objects.md`](information-objects.md).

The `dg:` instance-level individuals (`ext:doc-acme-q3`, `ext:file-acme-pdf`,
`ext:quote-3f7a9c`) are typed *only* with the docgraph class — `rdf:type dg:Document`
etc. They don't carry an explicit `rdf:type iso15926:ArrangedIndividual` triple; that
follows transitively from `dg:Document rdfs:subClassOf iso15926:ArrangedIndividual` and
is materialised by any reasoner.

## Built-in modality individuals (RFC 2119 as docgraph individuals)

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
