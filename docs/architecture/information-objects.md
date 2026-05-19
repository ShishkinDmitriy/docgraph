# Information objects: file → document → chapter → quote chain

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

## Concrete shape (turtle)

Uses the `dg:` structural classes defined in [`meta-ontology.md`](meta-ontology.md)
(`dg:PdfFile`, `dg:Document`, `dg:Chapter`, `dg:Quote`) plus a domain-specific
subtype of `dg:Document` for the report kind:

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
"PDF → Markdown derivation" below.

## Design rules

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
   may survive (they get repaired per the cascade-delete rules in
   [`provenance.md`](provenance.md)).

## PDF → Markdown derivation

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
