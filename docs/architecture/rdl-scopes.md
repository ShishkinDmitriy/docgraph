# RDL scopes — `add`, `enrich`, `consolidate`

Every class in the graph belongs to an **RDL** (Reference Data Library) at
some scope. Scopes form a hierarchy of authority, narrowest to widest:

```
doc-local  →  project-local  →  consortium  →  regional standard  →  world standard
  (one PDF)    (this project's    (CompanyX's    (German tax        (ISO 15926 Part 14,
               ext: namespace)    shared vocab)   ontology)         schema.org, FIBO, …)
```

URIs by scope:

| Scope | URI pattern | Owned by |
|---|---|---|
| doc-local | `urn:docgraph:source:<doc-slug>/<Class>` | docgraph (per doc) |
| project-local | `urn:docgraph:vocab:ext#<Class>` | docgraph (per project) |
| consortium / regional / world | whatever the upstream RDL declares | external authority |

docgraph **owns** the first two scopes and can mint/retire classes in them.
Higher scopes are read-only — we can reference their URIs, but never mutate
their definitions.

A class lives at exactly one scope. Cross-scope relationships are expressed
via `owl:equivalentClass` + `dcterms:isReplacedBy` (see "Deprecation" below).

## Three pipeline operations, one principle

The principle: **`add` is scope-local. Cross-scope work happens in `enrich`
and `consolidate`, never silently during ingest.**

### `add` — purely doc-local

Reads only the doc itself + the *currently loaded* RDLs (LIS-14 +
docgraph meta + any registered ontologies). Writes only into the doc's own
namespace (`urn:docgraph:source:<doc-slug>/...`).

The mega-extraction LLM sees existing classes at scope ≥ project-local
(those are stable, shared, safe to reuse). It does **not** see other docs'
doc-local proposals — they're invisible across docs by design.

Re-running `add` on the same input produces the same output (modulo LLM
nondeterminism). No cross-doc reads, no ordering effects between docs.

### `enrich` — instance retyping toward higher-scope classes

Operates on **instances**. For each entity in the graph, scan loaded RDLs
at scope ≥ this entity's current class scope; if a higher-scope class fits
the entity's semantics, retype the instance to it.

Example: an entity typed `ext:Invoice` (project-local) gets `enrich`ed
against LIS-14; if LIS-14 declared an equivalent (it doesn't today, but
hypothetically), the entity becomes typed `lis:Invoice` instead. The
`ext:Invoice` *class definition* is unaffected (that's `consolidate`'s
job) — only the instance triple is rewritten.

Triggers: explicit `docgraph enrich`. Doesn't run during `add`.

### `consolidate` — class merging across scopes

Operates on **class definitions**. For each class at scope N, search all
loaded scopes for semantic equivalents; consolidate onto the highest-scope
canonical.

Two cases, one operation:

1. **Mint upward** — multiple doc-local classes with the same meaning, no
   upstream equivalent → mint a canonical at scope N+1.
   Example: `urn:.../source:doc-1/Invoice` + `urn:.../source:doc-2/Invoice`
   + `urn:.../source:doc-3/Bill` (3 docs, ≥threshold) → mint
   `ext:Invoice` at project scope, mark the three doc-local URIs deprecated.

2. **Retire upward** — a class at scope N has an equivalent already at scope
   M > N (e.g., a new RDL got registered) → mark scope-N class deprecated,
   pointing at the scope-M canonical.
   Example: `ext:Invoice` (project) gets a new upstream equivalent
   `lis:Invoice` (world) → `ext:Invoice` is deprecated, instances retyped
   to `lis:Invoice`.

Both cases use the **same deprecation triple set** and the **same delta
mechanism** to rewrite contributing-doc instance triples.

## Deprecation pattern

When `consolidate` decides class `X` should be retired in favor of canonical
`Y` at higher scope:

```turtle
X
    a                       owl:Class ;
    owl:equivalentClass     Y ;       # semantic claim (reasoner-visible)
    owl:deprecated          true ;    # OWL 2 standard: prefer the replacement
    dcterms:isReplacedBy    Y ;       # directional pointer
    rdfs:label              "..." .   # original label preserved
```

Three triples carry distinct meaning:

- `owl:equivalentClass` — symmetric semantic claim (reasoners use this).
- `owl:deprecated true` — operational claim ("you can still use this URI,
  but prefer something else").
- `dcterms:isReplacedBy` — directional pointer to the canonical URI.

All instance triples typing as `X` are rewritten to type as `Y` in the
same `consolidate` delta (so the graph never has "deprecated but still has
live instances" as a transient state).

This pattern is W3C-standard (Dublin Core, SKOS, schema.org, OWL spec all
use it). Reasoners, serializers, viewers know what to do with it.

## Lifecycle invariants

1. **A class definition never disappears silently.** It either exists (live)
   or exists+deprecated (with a forward pointer to its replacement).
2. **Instance triples never reference a class that doesn't exist in the
   loaded graphs.** Deprecation marks the class; rewriting happens in the
   same delta; the two are atomic from the consumer's perspective.
3. **Removal requires explicit operator action.** `docgraph consolidate
   --gc` would physically delete deprecated class definitions whose
   replacement has fully absorbed all instance triples. Default behavior:
   never GC — deprecation triples are tiny, and they serve as a permanent
   audit log ("this class used to exist at this scope, was found equivalent
   to that one in delta N").

## Idempotency + scheduling

`consolidate` is idempotent. Running it twice in a row produces no change
on the second run. So "background process that merges equivalents" is just
"run `consolidate` periodically" — via cron, CI hook, post-ingest trigger,
or manual invocation. The deprecation triples carry the persistent state
between runs.

A typical workflow:

```sh
docgraph add doc-1.pdf      # local-only extraction
docgraph add doc-2.pdf      # local-only extraction
docgraph add doc-3.pdf      # local-only extraction
docgraph consolidate         # finds cross-doc equivalents, mints project canonicals
docgraph enrich              # retypes instances toward LIS-14 / other loaded RDLs
```

Each step has a single, narrow responsibility. Re-running any step is safe;
running them in different orders produces the same end-state graph (modulo
the order in which canonical URIs get minted).

## What this replaces

Pre-refactor, the `dedup` step (in `extract_part14/pipeline.py`) ran during
`add`, did cross-doc reads, and rewrote the new doc's URIs to point at
other docs' URIs. That broke `add`'s scope-locality and made re-running
`add` order-dependent. It's superseded by `consolidate` running explicitly.

`promote` (today's slug-based aggregation) becomes the first case of
`consolidate` — "mint upward" via slug collision. The embedding compare +
LLM relation classifier (today in `ext_dedup.py`) moves into `consolidate`
to also handle case 2 (different slugs, same meaning) and the cross-scope
"retire upward" case.
