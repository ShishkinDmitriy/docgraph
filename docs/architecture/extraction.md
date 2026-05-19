# Extraction — ontology-driven walker over the upper ontology

The extraction pipeline walks the loaded upper ontology's class hierarchy as a
decision tree. Each top-level class is a branch; the document is "extracted"
by visiting branches relevant to its subject classification and asking, per
branch, what instances the document contains. Properties of those instances
are then extracted in a second stage scoped to each instance's supporting
quotes.

This file specifies the algorithm. [`ARCHITECTURE.md`](../../ARCHITECTURE.md)
introduces the framing and shows how it maps onto the CLI commands.

## Tree shape — derived from the loaded ontology

The tree's structure is **not hardcoded**. The walker computes it from the
loaded `Dataset` at runtime:

1. Walk `rdfs:subClassOf` to find top-level classes — those whose only
   super-class is `owl:Thing` or sits outside the loaded ontologies.
2. Each such class becomes a branch.

For LIS-14, this produces three top-level branches: `lis:Activity`,
`lis:Aspect`, `lis:Object`. Object subdivides into 5 children that are NOT
formally disjoint with each other (Person ⊆ Organism ⊆ PhysicalObject; a
Person can be a member of an Organization; a Pump is both FunctionalObject
and PhysicalObject — the overlap is real).

For Part 2 (legacy pipeline), the same walk would yield ~14 top-level classes
matching the existing per-aspect prompts in `src/classify_part2/`. Same
algorithm, different ontology, different tree.

**Switching upper ontologies** (Part 14 → some new release, or to ECLASS, or
to a domain ontology layered on top) **needs no code change** — the walker
re-computes the tree from whatever's loaded.

## Axioms beyond `subClassOf`

The walker reads several other OWL axioms from the loaded ontology to keep
extraction lean. These are encapsulated in `src/extract_part14/axioms.py`
helpers; the walker calls them, doesn't reimplement them.

| Axiom | Helper | Use |
|---|---|---|
| `rdfs:subClassOf` | `top_level_classes(ds)`, `subclasses(ds, c)` | Tree structure |
| `owl:AllDisjointClasses` / `owl:disjointWith` | `disjoint_with(ds, c)` | Skip entities of incompatible types in subsequent branches |
| `owl:inverseOf` | `inverse_of(ds, p)` | Extract one direction of an inverse pair only; derive the other |
| `rdfs:subPropertyOf` | `parent_property(ds, p)` | Don't extract both `hasArrangedPart` AND `hasPart` for the same entity |
| `rdfs:domain` | `properties_of(ds, c)` | Per-class property iteration in stage 2 |
| `rdfs:range` | `range_of(ds, p)` | Validate property values; constrain reference resolution |
| `rdfs:label`, `skos:definition`, `rdfs:comment` | `class_label(ds, c)`, `class_definition(ds, c)` | LLM-facing context for prompts |

For LIS-14 specifically: 1 `AllDisjointClasses` axiom (`Activity ⊥ Aspect ⊥
Object` — a partition of the top level, with disjointness inheriting via
subClassOf), ~12 `inverseOf` pairs, many `subPropertyOf` chains. Disjointness
not formally asserted between Object's 5 children — and shouldn't be (legitimate
overlap). Finer-grained pairs can be added incrementally to
`vendor/ontologies/dg-part14-alignments.ttl` when real data forces a decision.

## Three passes, all ontology-driven

Extraction breaks into three passes, each driven by an ontology walk + a
generic prompt template. There are **no per-branch prompt files** — the prompts
are templated and parameterized by class/property metadata pulled from the
loaded ontology.

### Pass 1 — Subject classification (one LLM call per document)

Picks one or more candidate classes from a small set: top-level classes from
the upper ontology, optionally one level deeper where the top is too generic
to be useful as a subject (e.g., for LIS-14 we go one level into `lis:Object`
since "Object" alone is too broad).

```python
def subject_candidates(dataset) -> list[Class]:
    """For LIS-14: Activity + Aspect + immediate children of Object,
    minus Object itself. ~7 candidates."""
```

Output: `<source> dg:isAbout <c1>, <c2>, …` plus `dg:subjectConfidence`. Used
by pass 2 to weight branch ordering / skip irrelevant branches.

### Pass 2 — Stage 1: entity extraction per branch (sequential)

For each top-level class B in the upper ontology (in subject-relevance order):

```python
already_typed = {}  # entity_uri → set of class_uris (grows as branches run)

for branch in walk_top_level_classes(dataset):
    excluded = {
        eid for eid, types in already_typed.items()
        if any(disjoint_with(dataset, branch).contains(t) for t in types)
    }
    candidate_entities = {eid: t for eid, t in already_typed.items() if eid not in excluded}

    result = llm_extract(
        prompt = ENTITY_EXTRACTION_TEMPLATE.format(
            class_label       = class_label(dataset, branch),
            class_definition  = class_definition(dataset, branch),
        ),
        markdown                  = full_markdown,
        existing_entities_context = format_existing(candidate_entities),
        existing_excluded_context = format_disjoint(excluded),
    )

    for entity, evidence_selectors in result:
        mint_entity(entity, type=branch)
        for selector in evidence_selectors:
            mint_quote(selector, supporting=entity)        # top-down — see below
        already_typed[entity.uri] = {branch}
```

Branches run sequentially, each pass enriched by what prior passes found.
Disjointness lookup excludes incompatible already-typed entities from each
branch's candidate context, so the LLM doesn't re-classify them and can
reference them by URI when they appear in this branch's content.

### Pass 3 — Stage 2: property extraction per entity (scoped)

For each entity extracted in pass 2:

```python
for entity in extracted_entities:
    properties = properties_of(dataset, entity.type)
    properties = filter_inverses(properties, dataset)            # extract one direction only
    properties = filter_subproperties_of(properties, dataset)    # specialized only

    quote_context = "\n\n".join(
        format_quote_with_window(q) for q in entity.supporting_quotes
    )
    standing_context = document_context_block                    # title, dates, headers

    for prop in properties:
        result = llm_extract(
            prompt = PROPERTY_EXTRACTION_TEMPLATE.format(
                entity_label       = entity.label,
                property_label     = property_label(dataset, prop),
                property_definition= property_definition(dataset, prop),
                expected_range     = range_of(dataset, prop),
            ),
            context = standing_context + "\n\n---\n\n" + quote_context,
        )
        if result.value:
            add_property(entity, prop, result.value)
```

The full markdown is **not** sent here — only the entity's supporting quotes
(with a small context window from `oa:prefix` / `oa:suffix`) plus a stable
document-context block (title, dates, party / sender, key headers — extracted
once at M1 time and cached). Property prompts cost ~hundreds of tokens each,
not thousands.

## Quote model — top-down, evidence-driven

A `dg:Quote` exists in the graph **only because some extracted fact cites it
as evidence**. There is no bottom-up "mint a quote per markdown paragraph"
pass. Implications:

- `---` separators, bare headings, and other structural-but-empty markdown
  never become quotes (nothing cites them as evidence).
- Quote count scales with extracted-fact count, not document length.
- Quotes have at least one `<entity> oa:hasSelector <quote>` (or whichever
  reverse-link relationship is used) — they're never dangling.

Quotes use the **W3C Web Annotations Data Model** (`oa:` namespace) — the
standard vocabulary for "selectors into a document":

```turtle
<quote-abc123>  a dg:Quote, lis:InformationObject ;
    oa:hasSource <urn:dgcache:zahnrechnung2025-md> ;
    oa:hasSelector [
        a oa:TextQuoteSelector ;
        oa:exact   "für zahnärztliche Leistungen erlaubte ich mir zu berechnen: EUR 115,84" ;
        oa:prefix  "Sehr geehrter Herr Shishkin,\n" ;        # 30-char window before
        oa:suffix  "\n| Datum | Region |" ;                  # 30-char window after
    ] .
```

The `prefix`/`suffix` make the selector robust to small markdown edits — you
can re-find the quote even if line numbers shift.

Quote URIs are content hashes (SHA-1 of `oa:exact`). This gives free
**cross-source dedup**: identical text supporting facts in two different
documents merges to one quote node with two reverse-links, one to each
document's entity. Within one source the same property of dedup applies — if
a single paragraph supports multiple facts, it's the same quote URI with
multiple reverse-links.

`oa:hasSource` points back at the markdown cache file (`.docgraph/cache/pdfmd/<slug>.md`),
so a viewer can scroll to the exact span. The graph never duplicates the text
beyond `oa:exact`.

## Document-bounded descent — stopping conditions

A branch's stage 1 extraction terminates when any of these fire:

| Condition | Detection | Action |
|---|---|---|
| **No further evidence** | LLM returns empty / "this document doesn't elaborate on this" | Stop branch — no entities emitted |
| **Only references survive** | Returned entities are bare names without descriptive content | Mint reference stubs in `_unresolved.ttl`; stop branch |
| **Confidence floor** | LLM's confidence < threshold (per-entity or branch-aggregate) | Stop branch; record `dg:lowConfidenceBranch` annotation |
| **Cost threshold** | Tokens spent > threshold and new-entity rate falling | Stop branch; record `dg:costBoundedBranch` annotation |

Stage 2 (property extraction) terminates per-property:

- The LLM says "no value for this property in the supporting quotes" → no triple
- The LLM proposes a value whose type/range doesn't match → reject (validation via `rdfs:range`)
- Confidence below threshold → no triple

The named graph for one document is bounded by these conditions. Anything
the document doesn't say stays out; references to entities the document
mentions but doesn't elaborate on become stubs. The walker doesn't recurse
beyond what the document supports.

## Stub-vs-extract decision

When stage 1 returns an entity, it lands in one of three states:

| State | Trigger | Storage |
|---|---|---|
| **Extracted** | Entity has descriptive content (properties, supporting quotes with body) in the document | Full triples in the source's named graph; quotes minted |
| **Reference-with-identifier** | Entity is named with a stable identifier (URI, registered name, code) but no further detail here | Reference triple in source's graph; stub in `_unresolved.ttl` if not yet defined elsewhere |
| **Reference-without-identifier** | Entity is mentioned in passing only ("a service was rendered"), no identifier | Local-only blank-node reference in source's graph; no stub |

The `_unresolved.ttl` mechanism (see [`provenance.md`](provenance.md) §
"Unresolved concepts") handles state 2: when a defining document arrives
later, the stub gets repaired and the reference resolves.

## Cross-document references

When document A asserts about entity X and document B also touches X:

- A's named graph: full triples about X (extracted)
- B's named graph: B's specific claims that touch X (also extracted; B may
  add more facts about X)
- Both graphs share the URI X — no copying

To follow chains across graphs, run SPARQL across the loaded `Dataset`. The
walker itself never crosses named-graph boundaries during extraction;
extraction is per-document, queries are cross-document.

## Caching — invalidates at the right grain

```
.docgraph/cache/extract/
  <markdown-hash>/
    subject.json                    ← pass 1 output
    branches/
      <branch-uri-slug>.json        ← pass 2 stage 1: entities + evidence selectors
    entities/
      <entity-uri-slug>.json        ← pass 2 stage 2: properties (one file per entity)
```

Cache invalidation:

- **Subject prompt changes** → invalidate `subject.json` + downstream branches (subject affects branch ordering / weighting).
- **Branch entity-extraction prompt changes** → invalidate that branch's `branches/*.json` + its entities' stage 2 cache.
- **Property-extraction prompt changes** → invalidate the affected entities' `entities/*.json` only (not stage 1).
- **Markdown changes** → invalidate everything (different document).
- **Ontology changes** → invalidate everything (different tree).

Iteration loops in practice:

| Iteration target | Re-runs |
|---|---|
| Property prompt for one property | N small LLM calls (N = entities of that type) |
| Property prompt for one entity type | Same — N small calls |
| Branch entity-extraction prompt | 1 large call for that branch + invalidates its entities' stage 2 |
| Subject classification prompt | 1 small call + invalidates everything downstream |

## Optional opt-out via ontology annotations

By default everything in the loaded ontology that has `rdfs:label` and
`rdfs:comment` / `skos:definition` is extractable. Two opt-out annotations
live in `vendor/ontologies/dg-part14-alignments.ttl` (or any user-loaded
ontology):

- `dg:extractable false` on a class → skip it as a branch / as a stage 2 type
- `dg:extractable false` on a property → never ask about it in stage 2
- `dg:promptHint "..."` on a class or property → augment the LLM context
  beyond what `rdfs:comment` provides

These should be exceptions, not the rule. Most classes and properties in a
well-curated upper ontology are extractable as-is.

## Implementation notes — for `src/extract_part14/`

- **`axioms.py`** — pure SPARQL/rdflib helpers over the loaded `Dataset`.
  No LLM calls, no I/O beyond the Dataset. The contract surface for the
  walker is small and easy to test.
- **`walker.py`** — the three-pass loop. Calls `axioms.py` for structural
  questions; calls `llm_client.create()` for each prompt; calls
  `recognize.py` for any template recognition that applies.
- **Prompts live as constants in code, not separate files**, until the
  prompt count grows beyond what's manageable inline (M2 has 3 prompts;
  no need for a `data/prompts/` directory yet).
- **The walker is a flat loop** over top-level branches; recursion-within-
  branch is bounded by stopping conditions. No unbounded depth.
- **The same machinery serves both pipelines** — the only thing that differs
  between Part 2 and Part 14 is the upper ontology loaded (and so the tree
  shape derived from it).
