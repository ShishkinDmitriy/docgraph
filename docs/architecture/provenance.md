# Provenance: named graphs + source-content reification

The project uses a two-layer provenance model:

1. **Named graphs** carry *source-level* provenance. Every triple lives in exactly one
   named graph. The graph URI *is* the source identifier. No per-triple `dg:definedBy`.
2. **Part 2 reification** (`Classification`, `Specialization`,
   `RepresentationOfThing`, `CompositionOfIndividual`, …) is used inside a graph
   when the *content* of the source asserts a fact whose temporal extent, authority,
   or context is part of the assertion (per the reification rule in
   [`meta-ontology.md`](meta-ontology.md)).

The two layers are complementary: named graphs answer *"who wrote this triple set"*,
reification answers *"who/when/by-what-authority does this specific fact hold"*.

## Permanent backbone — `meta.ttl`

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

## Document-sourced assertions

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

## Cascade delete

`docgraph remove <source>` drops the source's named graph file and its
`sources.ttl` entry. Triples in other graphs that referenced concepts the
source defined get repaired — `<x> rdf:type <removed-class>` rewrites to
`rdf:type iso15926:ArrangedIndividual` (when applicable) or drops; reified
relationship nodes pointing at removed concepts are dropped. The meta
backbone (`meta.ttl`) is never touched.

## TTL ingest is one parser among several

A `.ttl` source goes through the same pipeline as any other input: parse →
classifier → named graph. The TTL parser is just *cheaper* than the PDF
parser (no vision LLM step). The ingest stamps the registry with
`dg:addedAt` and one or more `dg:defines` values from structural inspection
(Classes, Properties, Individuals).

---

# DEFINE vs REFERENCE — ownership

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
merge conflict — see open questions in [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md).

## Unresolved concepts

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
