# Templates — Part 7-style lifted/lowered patterns

Templates are the **universal LLM-emit and storage-grounding mechanism**. Every
assertion the LLM produces is a template instance; every domain ontology is a
template library; every Part 2 reified cluster on disk is the *lowered form* of a
template. The lifted form is the LLM's vocabulary; the lowered form is the canonical
storage representation. Storage stays uniformly Part 2-shaped because every template's
lowered body is grounded to Part 2 (or as close as the template's author chose to go).

Three motivations:

1. **Compression at the LLM + human boundary.** A `SourcedAssertion` is one named
   bundle of {document, quote text, locator, references}; the equivalent reified
   Part 2 form is ~5 nodes / ~13 triples. The LLM emits the bundle as JSON; the
   engine expands.
2. **Domain ontologies as template libraries.** Part 2 has no `dom:hasVatNumber` —
   the predicate exists only as the lifted form of a template whose lowered body
   says, in proper Part 2 shape, what the assertion really is. The financial domain,
   the procurement domain, the equipment domain — each is a directory of templates
   under `data/templates/`, not a separate OWL ontology with hand-rolled
   `owl:DatatypeProperty` declarations. See "Domain ontologies as template
   libraries" below.
3. **Foreign ontologies as bridge libraries.** PROV-O, schema.org, and similar
   external vocabularies become *bridge templates* whose lifted form is the foreign
   idiom and whose lowered form is the equivalent reified Part 2 cluster. This
   subsumes Phase 3 anchors (a 1-triple-↔-1-triple template) and Phase 2 lift rules
   (a foreign-pattern-↔-canonical-pattern template) as degenerate cases.

Templates are first-class **as definitions** (URIs, files, registry, inspectable,
version-controlled) but **not as stored instances** — a template-instance is expanded
to reified Part 2 before being written to a graph file. We can flip to "store as
templates, materialize Part 2 on demand" later without losing data; starting with
expanded storage keeps every existing consumer working unchanged.

There is no separate "raw triple" emit path. Even a one-line datatype assertion
(`ext:invoice-001 dom:hasVatNumber "DE…"`) is a template instance — its template
just happens to have a 1-triple lowered body (a *pass-through* template; see "The
reification spectrum" below). Uniformity wins over special-casing.

## Lifted vs lowered (Part 7 terminology)

Borrowed verbatim from ISO 15926 Part 7:

- **Lifted form** — the compact representation. For instance-form templates, a typed
  instance with slot values (`var:this a tpl:Foo ; <slot-N> var:slot-N`). For pattern-form
  templates, an arbitrary graph pattern (often a single triple, e.g.,
  `var:x prov:wasGeneratedBy var:y`).
- **Lowered form** — the expanded reified Part 2 graph. The canonical storage form.

Every template declares both as RDF graphs that share a set of variables. Engine
operations:

- **Expansion** — match lifted, substitute into lowered. Used at extraction time:
  the analyzer's emit format is template instances; the engine writes reified Part 2
  to the graph file.
- **Recognition** — match lowered, substitute into lifted. Used at display time
  (inspector folds reified clusters back to template form). Also useful when
  ingesting foreign Part 2 data not authored as templates.

Both directions use the same machinery (subgraph match + variable substitution).
No embedded SPARQL strings; no separate rule pairs.

## Two declaration shapes

| Shape | Lifted form | Declaration |
|---|---|---|
| **Instance-form** | Typed instance with named slots: `var:this a tpl:Foo ; <slot-N> var:slot-N` | `tpl:slot` list only; lifted graph auto-derived |
| **Pattern-form** | Arbitrary graph pattern (often 1 triple): `var:a prov:rel var:b` | Explicit `tpl:lifted` named graph; no `tpl:slot` |

Most templates are instance-form (the Part 7 default). Pattern-form is for bridge
templates and others where the lifted form is just a foreign vocabulary's idiomatic
shape — there's no anchor node to attach slots to.

## Variables and the `var:` namespace

Template files use a single CURIE prefix `var:` mapped to `urn:tpl-var/` for **all**
variables — slot variables, named intermediates, and the lifted/lowered graph URIs.
Authors write `var:doc`, `var:quote`, `var:lowered`, etc.; the loader skolemizes
every `urn:tpl-var/X` to a per-template URI `urn:tpl/<slug>/var/X` at parse time,
where `<slug>` is the kebab-case local-name of the template URI (e.g.
`SourcedAssertion` → `sourced-assertion`). This avoids cross-template aliasing if
multiple templates ever share an in-memory dataset.

Variable roles are determined by usage, not by an explicit type annotation:

- **Slot variable** — listed under `tpl:slot`. The slot's name is the URI's local-
  name; metadata (`tpl:range`, `tpl:minCount`, `tpl:maxCount`) attaches via the
  same URI as subject. The slot's URI is the variable in the lowered graph — one
  node, two roles, no separate `tpl:name` string needed.
- **Named intermediate** — appears in the lowered graph but not under `tpl:slot`
  (e.g., `var:quote` in `tpl:SourcedAssertion`). Treated as identity-stable: one
  per template instance, shared across iterations of a multi-valued slot.
- **Anonymous** — written as `[ ... ]` blank nodes in the lowered body. The loader
  rewrites them to URIs in `urn:tpl/<slug>/anon/`. Per-iteration when reachable
  from the multi-valued slot through anon-only edges; otherwise stable.

## Instance-form template — example

```turtle
@prefix tpl:      <http://example.org/docgraph/template#> .
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix var:      <urn:tpl-var/> .

dg:SourcedAssertion a tpl:Template ;
    rdfs:label  "Document quote describing things" ;
    tpl:definition "[doc] asserts via [quoteText] at [locator] that [references] is the case." ;
    tpl:subject iso15926:Description ;
    tpl:slot var:doc, var:quoteText, var:locator, var:references ;
    tpl:lowered var:lowered .

var:doc        tpl:range dg:Document .
var:quoteText  tpl:range xsd:string .
var:locator    tpl:range xsd:string .
var:references tpl:range iso15926:Thing ;
               tpl:maxCount 0 .                                  # 0 = unbounded

GRAPH var:lowered {
    var:quote a dg:Quote ;
              dg:text    var:quoteText ;
              dg:locator var:locator .
    [ a iso15926:CompositionOfIndividual ;
      iso15926:hasWhole var:doc ;
      iso15926:hasPart  var:quote ] .
    [ a iso15926:Description ;
      iso15926:hasSign        var:quote ;
      iso15926:hasRepresented var:references ] .
}
```

The lifted graph is *not written* — it's derived at template-load time from the slot
list:

```turtle
# implicit, auto-built; per-template namespaces shown post-skolemization
GRAPH <urn:tpl/sourced-assertion/var/lifted> {
    var:this a dg:SourcedAssertion ;
             <urn:tpl/sourced-assertion/slot/doc>        var:doc ;
             <urn:tpl/sourced-assertion/slot/quoteText>  var:quoteText ;
             <urn:tpl/sourced-assertion/slot/locator>    var:locator ;
             <urn:tpl/sourced-assertion/slot/references> var:references .
}
```

Slot-property URIs (`urn:tpl/<slug>/slot/<name>`) are synthesised per template and
never need to be typed by humans — template instances are written as JSON by the LLM
and expanded by the engine. The slot name (`doc`, `quoteText`, …) is just the local-
name of the slot's `var:` URI; no separate `tpl:name` string is needed.

`tpl:definition` is a natural-language sentence with `[slot]` placeholders, borrowed
directly from Part 7's `<Definition>` element. It's the LLM-facing summary —
explains what the template means and where each role appears.

## Pattern-form template — example (PROV-O bridge)

```turtle
@prefix tpl:  <http://example.org/docgraph/template#> .
@prefix prov: <http://www.w3.org/ns/prov#> .
@prefix var:  <urn:tpl-var/> .

<urn:tpl/prov-wgb> a tpl:Template ;
    rdfs:label "PROV-O wasGeneratedBy" ;
    tpl:subject iso15926:CompositionOfIndividual ;
    tpl:lifted  var:lifted ;
    tpl:lowered var:lowered .

GRAPH var:lifted {
    var:entity prov:wasGeneratedBy var:activity .
}
GRAPH var:lowered {
    [ a iso15926:CompositionOfIndividual ;
      iso15926:hasWhole var:activity ;
      iso15926:hasPart  var:entity ] .
}
```

No slot list; matching is direct against the lifted graph pattern. Variables
(`var:entity`, `var:activity`) are shared across the two named graphs by URI
identity (post-skolemization).

The fully degenerate case is 1-triple-↔-1-triple (e.g., lifted `var:x a prov:Activity`,
lowered `var:x a iso15926:Activity`). That's exactly a Phase 3 anchor expressed as a
template — see "Relationship to existing pipeline phases" below.

## The reification spectrum: pass-through to fully reified

The lifted form is the LLM's vocabulary; the lowered form is the canonical storage
representation. The author chooses how reified the lowered body is — there's a
spectrum, not a binary:

| Reification level | Lowered body | When |
|---|---|---|
| **Pass-through** (1 ↔ 1) | Same triple as lifted: `var:x dom:p var:y` → `var:x dom:p var:y` | Static structural attachments where reification isn't worth the cost — same logic as the existing `rdf:type` vs reified-`Classification` rule. The default for datatype properties (`dom:hasVatNumber`, `dom:hasIssueDate`, etc.) |
| **Lightly reified** | A 2–3 triple `Identification` or `Description` tuple wrapping the value | When the value carries source/time/authority that should be queryable separately from the value itself |
| **Fully Part 2-reified** | The complete cluster (`Identification` + `ClassOfInformationRepresentation` carrying the literal + temporal extent + authority) | Strict Part 2 stance, or when the value is itself a sourced/temporal claim |

The decision criterion is the same one already documented in "When to reify, when
to use plain RDFS — the docgraph rule": reify when the assertion carries
information that **shouldn't be true at all times** or has a **specific
source/authority worth preserving beyond the named-graph level**. Otherwise
pass-through.

**Cost reality.** A typical invoice with 20 datatype-property assertions:
- All pass-through → 20 triples (cheapest; no Part 2 grounding for those values)
- All lightly reified → ~60 triples (3× expansion)
- All fully Part 2-reified → ~100 triples (5× expansion)

Plus the quote chain (~13/quote) and any reified relationships. Default to
pass-through for datatype properties; reify selectively where the value's
provenance matters.

## Multi-valued slots

Slots have SHACL-compatible cardinality (`tpl:minCount`, `tpl:maxCount` with `0`
meaning unbounded). When a slot is multi-valued, expansion iterates over the value
set, emitting one set of substituted lowered triples per value. The
`tpl:SourcedAssertion` quote-with-N-references case is canonical — one quote node, N
description tuples sharing the quote.

**Trap to avoid: at most one multi-valued slot per template.** Two multi-valued slots
in the same template trigger SPARQL-style cross-product semantics (`{a,b} × {x,y}`
→ 4 expansions, not 2), which is almost always wrong. If paired multi-values are
needed, model as a sub-template per pair or as an RDF-list-valued slot.

## Sub-template composition

A template's lowered body can invoke other templates instead of open-coding their
reified clusters. Leaf templates (e.g., `tpl:CompositionPart`, `tpl:Description`)
are the only places raw Part 2 reified triples appear; everything else composes
leaves.

```turtle
GRAPH var:lowered {
    var:quote a dg:Quote ; dg:text var:quoteText ; dg:locator var:locator .

    [ a tpl:Invocation ; tpl:invokes tpl:CompositionPart ;
      tpl:bind [ tpl:role "whole" ; tpl:value var:doc ] ,
               [ tpl:role "part"  ; tpl:value var:quote ] ] .

    [ a tpl:Invocation ; tpl:invokes tpl:Description ;
      tpl:bind [ tpl:role "sign"        ; tpl:value var:quote ] ,
               [ tpl:role "represented" ; tpl:value var:references ] ] .
}
```

**Resolution is at load time, not runtime.** The template loader recursively expands
invocations into the final flat lowered body once, when the template is registered.
The runtime engine never sees `tpl:Invocation` nodes — it only matches/substitutes
fully-expanded leaf-level Part 2 patterns. Composition is pure authoring convenience
with no runtime cost or recursion concerns.

## Deterministic URI minting

Lowered bodies introduce intermediate nodes (the `var:quote`, the reified
`Composition` and `Description` blank nodes). Each must get a stable URI per
template-instance so re-expansion is idempotent and cascade-delete is sane.

Recipe: hash of `(template-instance-anchor, lowered-graph-bnode-id, slot-bindings)`.
URI minting happens in the engine outside the template body — the body declares
intermediate nodes by structure, the engine substitutes URIs at expansion time.
Templates don't carry minting logic.

## Recognition — lowered → lifted via SPARQL

The recognition direction (matching stored Part 2 against a template's lowered
body) is implemented by **translating the lowered graph to a SPARQL `SELECT *
WHERE { … }` query at runtime**, running it against the input graph via rdflib,
and folding the result rows into per-instance binding dicts. No SPARQL is stored
on disk — templates remain declarative; SPARQL is an execution detail.

The translation is purely structural:

| Lowered-graph term | SPARQL form |
|---|---|
| Slot variable (e.g., `urn:tpl/<slug>/var/doc`) | `?doc` (projected) |
| Named intermediate (e.g., `urn:tpl/<slug>/var/quote`) | `?quote` (projected as well, since `SELECT *` is used; not part of the lifted-form output) |
| Anon URI (e.g., `urn:tpl/<slug>/anon/_b0`) | `?anon_b0` — dedicated `anon_` prefix so they can never collide with slot names |
| Concrete URI / literal | Emitted as a CURIE if the source TTL declared a matching `@prefix`, otherwise as a full `<…>` URI |

Source-file `@prefix` declarations are captured at load time onto
`Template.prefixes`, with the `var:` prefix dropped (its URIs are skolemized away).
The query emits a `PREFIX X: <Y>` declaration only for prefixes whose CURIEs are
actually used in the BGP, so unused prefixes from the source file don't leak in.
Triples in the BGP are emitted in (s, p, o)-sorted order so the generated query
is deterministic across runs (handy for review and for golden-file tests).

**Multi-valued slot folding.** Storage shaped by expansion has, for an N-valued
slot, one shared head plus N tuples that touch the multi-slot variable. The
SPARQL query thus matches N rows per instance, each row carrying a different
multi-slot binding but identical non-multi bindings. The recognizer **groups
result rows by the tuple of non-multi bindings** and collects the multi-slot
values into a list — turning N rows back into one instance-form binding dict
whose multi-valued slot is a list. Pattern-form templates skip the slot-grouping
logic and return one binding dict per match keyed by the lifted-graph variable
local-names.

Generated SPARQL for `tpl:SourcedAssertion`:

```sparql
PREFIX dg: <http://example.org/docgraph/meta#>
PREFIX iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

SELECT * WHERE {
  ?anon_b0 iso15926:hasPart ?quote .
  ?anon_b0 iso15926:hasWhole ?doc .
  ?anon_b0 rdf:type iso15926:CompositionOfIndividual .
  ?anon_b1 iso15926:hasRepresented ?references .
  ?anon_b1 iso15926:hasSign ?quote .
  ?anon_b1 rdf:type iso15926:Description .
  ?quote dg:locator ?locator .
  ?quote dg:text ?quoteText .
  ?quote rdf:type dg:Quote .
}
```

A storage graph holding two SourcedAssertion clusters (one with two references,
one with three) recognizes back as exactly two instances whose `references` slots
are lists of size 2 and 3. Verified by `tests/test_template_recognize.py`.

The translator and recognizer live in `src/templates/recognize.py`; golden-file
tests under `tests/fixtures/templates/<stem>.sparql` lock in the exact output
per template fixture for review. Regenerate after intentional translator
changes via the one-liner in the test file's header comment.

## Validation — SHACL derived on the fly

Same pattern as modality (see "Modality and SHACL derivation" in
`ARCHITECTURE.md`): SHACL is **not stored** as part of template definitions.
When validation is needed (LLM-emitted template instance gets type-checked
before expansion), a SHACL shape is derived from the slot list at runtime:

```python
def derive_shacl_for_template(template):
    for slot in template.slots:
        yield PropertyShape(
            path=mint_slot_predicate(template.uri, slot.name),
            class_=slot.range if slot.is_object else None,
            datatype=slot.range if slot.is_literal else None,
            min_count=slot.min_count or 1,
            max_count=slot.max_count if slot.max_count != 0 else None,
        )
```

Slot semantics (`tpl:minCount`, `tpl:maxCount`, `tpl:range`) deliberately mirror
SHACL's so the bridge is mechanical. Custom vocabulary names are kept rather than
reusing `sh:` because slots are named by string, not by URI path — `sh:property` /
`sh:path` would force per-slot predicate URIs at the declaration site, which we
avoid for authoring lightness.

## LLM is the template's primary user

The LLM emits a uniform **list of template instances** — there is no separate
raw-triple emit path. Every assertion (datatype property values, type
classifications, sourced quotes, relationships) is a template instance:

```json
{
  "instances": [
    { "template": "dom:InvoiceFormClassification",
      "bindings": { "this": "ext:invoice-001", "form": "dom:Invoice" } },
    { "template": "dom:InvoiceHasVatNumber",
      "bindings": { "invoice": "ext:invoice-001", "value": "DE123456789" } },
    { "template": "dom:InvoiceHasIssueDate",
      "bindings": { "invoice": "ext:invoice-001", "value": "2026-04-15" } },
    { "template": "tpl:SourcedAssertion",
      "bindings": { "doc": "ext:doc-acme-invoice",
                    "quoteText": "VAT ID DE123456789, issued 15 April 2026",
                    "locator": "p.1",
                    "references": ["ext:invoice-001"] } }
  ]
}
```

The first three instances have pass-through lowered bodies (each expands to a
single triple, identical to its lifted form). The fourth has a fully reified
lowered body (~13 triples — quote node, composition tuple, description tuple). The
LLM doesn't see this difference; it just emits template instances. The engine
handles expansion uniformly.

Prompts include `{template URI, definition string with [slot] placeholders, slot
list with ranges, examples}` per available template — derived directly from
template definitions. The LLM never sees lowered bodies; expansion is engine
territory.

The reliability win: errors become slot-shape errors (validatable via the derived
SHACL) instead of malformed reifications, and the LLM doesn't need to choose
between "emit raw triples" and "emit reified clusters" — there's only one path.

## Storage layout

Templates ship in three locations, all using the same on-disk format (one TTL per
template):

```
data/templates/                 ← built-in core templates (sourced-assertion,
                                   classification-by-authority, composition-part,
                                   description, …)
data/templates/bridges/         ← bridge ontologies as template libraries
                                   (prov-o/, schemaorg/, sosa/, …)
.docgraph/cache/templates/      ← LLM-discovered templates, user-approved
                                   (same gate as cache/lifts/, cache/anchors/)
.docgraph/templates.ttl         ← registry: which templates are loaded, source
                                   path, version
```

Built-ins live in the repo. Bridge libraries live in subdirectories so the user can
opt into specific bridges (`docgraph templates enable prov-o`). Cache entries are
per-template, keyed by template URI.

One file per template — same logic as one source per `graphs/<slug>.ttl`: easy to
inspect, diff, add, remove, version-control.

## Domain ontologies as template libraries

Domain ontologies (financial, procurement, equipment, etc.) are template
libraries, not standalone OWL files. The repo ships canonical domains under
`data/templates/<domain>/`, each containing one TTL per template — pass-through
templates for datatype-property predicates, instance-form templates for
multi-slot bundles, and reified templates wherever the assertion carries
provenance worth surfacing.

The financial domain becomes:

```
data/templates/financial/
  invoice-form-classification.ttl       ← pass-through: var:x a dom:Invoice
  invoice-has-vat-number.ttl            ← pass-through: var:x dom:hasVatNumber var:v
  invoice-has-issue-date.ttl            ← pass-through: var:x dom:hasIssueDate var:d
  invoice-has-line-item.ttl             ← instance-form: bundles party, amount, date
  payment-confirmation-classification.ttl
  …
```

Each template carries its own modality and SHACL-derivable constraints (see
"Modality and SHACL derivation" in `ARCHITECTURE.md` — modality lives on the
template declaration, not on a separate `owl:DatatypeProperty` declaration). The
OWL `owl:Class` / `owl:DatatypeProperty` / `rdfs:domain` / `rdfs:range`
declarations that previously lived in `financial_documents.ttl` are absorbed: a
template's `tpl:slot` declaration carries the type info; `dom:Invoice` is a class
*because* it's the lifted-form type of the form-classification template.

**For OWL tool compatibility:** the template loader can synthesize the equivalent
flat OWL triples (`dom:hasVatNumber a owl:DatatypeProperty ; rdfs:domain dom:Invoice ;
rdfs:range xsd:string`) and add them to the meta graph automatically. External SPARQL
queries against the lifted vocabulary still work without knowing anything about
templates. This synthesis is mechanical (same data, different shape) and lives in
the loader, not in template files.

This is the deepest consequence of the unification: there's no "domain ontology"
TTL to ingest separately from "templates". Adding a domain to the system is
adding a directory of templates. `docgraph` doesn't need a separate
`add-ontology` command versus `add-template` — they're the same operation.

## Template discovery and filling — three sources of templates

Templates enter the system from three places, in increasing order of trust
required. Each source feeds a different gate; mixing them in one namespace would
conflate trust levels and corrupt the canonical layer.

| Source | Namespace | Storage | Trust |
|---|---|---|---|
| **Library** — curated, shipped with docgraph or with a domain | `tpl:`, `dom:` | `data/templates/` (in repo) | High |
| **Structural** — lifted deterministically from a document's own repetition (a 30-row table is one schema applied 30 times) | `cand:` | `.docgraph/cache/templates/structural/` | Medium — schema is real, slot semantics need review |
| **Learned** — proposed by the candidate-pattern index when a fact-shape recurs across documents (or directly by the LLM during extraction) | `cand:` | `.docgraph/cache/templates/learned/` | Low — promoted on recurrence + user approval |

Per-document processing is two-phase: (1) fold the extracted facts against
existing templates by recognition; (2) feed the un-folded remainder to the
discovery mechanisms below.

### Filling — subject-typed candidate selection

Library templates carry a **subject** — the Part 2 reified-relationship class
their lowered body anchors on (`iso15926:Activity`, `iso15926:Possession`,
`iso15926:Classification`, `iso15926:CompositionOfIndividual`, …). The
15926.blog template list and the Part 7 standard catalog are both organized
this way: a template "is about" the kind of relationship it reifies.

Subject-typing turns LLM extraction from open-ended template emission into
constrained slot-filling:

1. **Classify the fragment** (lightweight pass): "this paragraph describes an
   activity", "this row is a role assignment", "this attribute is a possession".
2. **Look up subject-indexed templates**: pull the candidate set for that
   subject (e.g., all activity templates).
3. **Fill**: the LLM picks one (or a few) and emits slot bindings. The derived
   SHACL (see "Validation" above) catches errors before expansion.

The classifier step is cheap, and the payoff is much higher fill accuracy — the
LLM chooses from the subject-relevant subset rather than the entire template
registry per fragment. Index shape: `subject → [template URIs]`, built at
template-load time from each template's `tpl:subject` annotation.

### Bootstrap from document structure (state-0)

When the graph is empty and the first document arrives, structural repetition
*in the document itself* is the cheapest source of templates. Tables, diagrams,
and any visually-repeated layout are already template instantiations: a 30-row
table is one schema applied 30 times. No pattern mining is needed at state-0 —
the repetition is explicit in the document's layout.

Extractor responsibilities:

- Markdown extractor surfaces tables as `(header-row, body-rows)` pairs; each
  becomes one structural-template candidate plus N instantiations.
- PDF extractor inherits whatever structure the converter preserved.
- Future structured extractors (XBRL, UBL XML, etc.) can lift schema directly
  from the source's own type declarations.

Each candidate template lands in `.docgraph/cache/templates/structural/` with
the document's quote chain attached as provenance. User reviews, names slots,
decides what's promotable. Promotion moves it to a library namespace.

This avoids the cold-start problem entirely: state-0 ingestion produces both
Part 2 facts *and* a starter template library derived from the document's own
structure.

### Cross-document discovery — candidate pattern index, not pairwise mining

For repetition that isn't visually obvious (recurring fact patterns across
documents), the naive approach — compare each new document pairwise to every
prior one — is O(n²) and quickly impractical. The right shape is a **candidate
pattern index** maintained incrementally:

1. **Signature extraction**: for each named graph after expansion, compute
   structural signatures of small subgraphs (bounded-depth walks of typed nodes
   connected by reified-relationship clusters). Signatures are content hashes
   of the type-shape, ignoring URIs of individuals.
2. **Index increment**: each signature gets a counter and a back-reference to
   the sources it came from. New document → O(s) signature increments where s
   is the document's signature count, not O(n).
3. **Promotion gate**: a signature whose count crosses a threshold (default
   `k=3` across at least 2 sources) becomes a *candidate template*. The engine
   reconstructs the lifted form from the type-shape and surfaces it for review.
4. **Approval**: same gate as LLM-discovered templates — user names the slots,
   confirms the lowered body, then it lands in
   `.docgraph/cache/templates/learned/`.

The frequent-subgraph-mining literature (gSpan, FSG) is the formal version;
this is the practical degenerate case where signatures are bounded-depth walks
and the index is a counter. Index file: `.docgraph/cache/pattern-index.ttl`,
survives across `docgraph add` invocations. `docgraph templates suggest`
surfaces candidates above threshold.

A learned template from one document is just a structural template from that
document's repetition (case 2 above). What distinguishes a learned template is
multi-source recurrence — the same shape showing up across two German invoices
and one Italian one is evidence of a real domain pattern, not one author's
idiom.

Until promoted, candidate templates may be expanded against a source for
testing but do not appear in the LLM's prompt vocabulary — keeps the canonical
filling layer free of unvalidated shapes.

## Cascade behaviors

- **Removing a template definition** — expansion stops being available; expanded
  Part 2 in graph files stays valid (it's self-contained reified triples). Optional
  `tpl:wasInstantiatedFrom` breadcrumbs become dangling references but don't break
  anything. Recognition can no longer fold the cluster back to template form.
- **Removing a source that contained template instances** — same cascade as any
  source removal: drop the named graph, drop its expanded Part 2 triples and any
  breadcrumbs.
- **Replacing a template with a new version** — open question (see "Open questions"
  in `ARCHITECTURE.md`).

## Relationship to existing pipeline phases

Templates subsume four existing analyzer mechanisms:

| Existing | Template equivalent |
|---|---|
| Phase 2 lift rules (`schema:domainIncludes` → `rdfs:domain`, etc.) | Pattern-form template: foreign idiom in lifted, canonical OWL in lowered |
| Phase 3 Part 2 anchors (`prov:Activity rdfs:subClassOf iso15926:Activity`) | Pattern-form template, 1-triple lifted ↔ 1-triple lowered |
| Domain ontology declarations (`owl:Class`, `owl:DatatypeProperty`, `rdfs:domain`/`range`) | Template files in `data/templates/<domain>/`, with OWL declarations synthesised at load time for tool compatibility |
| Step 8 property extraction (flat `{property → value}` JSON per form-class instance) | Each value emission becomes a pass-through template instance; the LLM's emit format is now a uniform list of template instances |

Migration is deferred — current Phase 2/3 and the property-extraction step keep
their implementations until templates land. Once templates exist, the migration
is mechanical (each lift rule, anchor, domain predicate, and form-class
declaration becomes a template). Post-migration, Phase 2 and Phase 3 collapse
into "load applicable templates and expand", and step 8 becomes "ask the LLM to
emit a list of template instances using the loaded vocabulary".
