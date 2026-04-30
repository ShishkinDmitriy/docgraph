# Classify pipeline — design notes

> Session: 2026-04-29. Replaces the single-call `classify_document_type` in
> `src/classify.py` with a 14-prompt pipeline that produces a graph of ISO
> 15926-2 entities for each document.

## Goals

- The output of `classify` is a knowledge graph whose nodes are typed with
  ISO 15926-2 (POSC Caesar OWL) classes.
- Cover the breadth of Part 2 (~150 of 201 entity types in the schema)
  through a small number of focused prompts rather than one monolithic prompt.
- Each prompt is gated by the previous one — only relevant prompts run.
- Reification idioms (Participation, Classification, Property+Scale,
  CompositionOfIndividual, …) are produced by a Python converter, not the
  LLM. Prompts emit semantic JSON; the converter turns it into Part 2 Turtle.

## Namespace and ontology

- Part 2 OWL: `http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#`
  - Local file: `docs/ISO-15926-2_2003.rdf` (303 declarations)
  - Annotations file: `docs/ISO-15926-2_2003_annotations.rdf`
    (provides `definition`, `note`, `example` per class)
- Class names use CamelCase: `Activity`, `ClassOfActivity`, `Participation`,
  `WholeLifeIndividual`, …
- Suggested Turtle prefix: `iso15926:` →
  `<http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#>`

`meta.ttl` `owl:imports` switches from `docs/LIS-14.ttl` to
`docs/ISO-15926-2_2003.rdf` + the annotations file.

## Part 2 structural quirks the converter must handle

Part 2 lacks individual-level classes for several common concepts. The
canonical encoding is "individual + ClassOf*" pairs:

| Concept | Individual class | Classifier (ClassOf*) |
|---|---|---|
| A document instance | `WholeLifeIndividual` | `ClassOfInformationObject` |
| A person | `WholeLifeIndividual` | `ClassOfPerson` |
| An organization | `WholeLifeIndividual` | `ClassOfOrganization` |
| A physical thing | `PhysicalObject` | `ClassOfInanimatePhysicalObject` |
| A functional thing | `FunctionalPhysicalObject` | `ClassOfFunctionalObject` |

Source-document typing follows the same pattern (decision: strict Part 2,
not a `dg:InformationObject` shortcut):

```turtle
<source>      a iso15926:WholeLifeIndividual ;
              a ext:Document .
ext:Document  a iso15926:ClassOfInformationObject .
```

Classification is reified as `iso15926:Classification`. Saying "John is a
Person" produces:

```turtle
ext:c-001  a iso15926:Classification ;
    iso15926:hasClassifier ext:Person ;
    iso15926:hasClassified ext:john-smith .
```

## Pipeline

```
                ┌── prompt 1 (always) ──┐
markdown ─────► │ Nature scan           │ ─► answers + doc_kind + subjects
                └───────────────────────┘
                            │
                  gates which of 2-14 run
                            ▼
                ┌── prompt 2 (Activities) ──┐
                │ if describes_activities   │
                ├── prompt 3 (Individuals) ─┤
                │ if describes_individuals  │
                ├── prompt 4 (Classes)      │
                │ if defines_classes        │
                │   or describes_activities │
                ├── prompt 5 (more Classes) ┤
                │ if defines_classes        │
                │   or describes_individuals│
                ├── prompt 6 (Roles)        ┤
                │ if describes_roles        │
                ├── prompt 7 (Participations)
                │ if activities AND         │
                │ (individuals OR roles)    │
                ├── prompt 8 (Whole-parts)  ┤
                │ if describes_whole_parts  │
                ├── prompt 9 (Temporal)     ┤
                │ if has_temporal_structure │
                ├── prompt 10 (Properties)  ┤
                │ if has_properties         │
                ├── prompt 11 (Numbers)     ┤
                │ if has_quantities         │
                ├── prompt 12 (IDs+Desc)    ┤
                │ if has_identifiers OR true│
                ├── prompt 13 (Connections) ┤
                │ if describes_connections  │
                └── prompt 14 (Lifecycle)   ┘
                  if has_lifecycle_or_approval
```

Each later prompt receives:
- the cached document markdown (prompt-cached, ~10% token cost),
- `doc_kind` and `primary_subjects` from prompt #1, and
- the entity ids/labels/summaries already extracted by earlier prompts,
  so it can attach relationships rather than re-mint nodes.

## Prompts and Part 2 sections

| #  | Prompt              | Part 2 §   | Skipped if            |
|----|---------------------|-----------|------------------------|
| 1  | Nature scan         | —         | (always runs)         |
| 2  | Activities & events | 5.2.9     | not `describes_activities`|
| 3  | Individuals         | 5.2.6, 5.2.7 | not `describes_individuals`|
| 4  | Classes of activity | 5.2.10    | not `defines_classes` AND not `describes_activities`|
| 5  | Classes of individual | 5.2.7, 5.2.8 | not `defines_classes` AND not `describes_individuals`|
| 6  | Roles               | 5.2.13, 5.2.24 | not `describes_roles`|
| 7  | Participations      | 5.2.9     | no activities OR (no individuals AND no roles)|
| 8  | Whole-parts         | 5.2.6 §4.7.1–2 | not `describes_whole_parts`|
| 9  | Temporal relationships | 5.2.22 | not `has_temporal_structure`|
| 10 | Properties          | 5.2.26, 5.2.27 | not `has_properties`|
| 11 | Numbers, scales, units | 5.2.5, 5.2.28 | not `has_quantities`|
| 12 | Identifiers & descriptions | 5.2.16 | not `has_identifiers` (still cheap to always run)|
| 13 | Connections         | 5.2.21    | not `describes_connections`|
| 14 | Lifecycle & approvals | 5.2.23  | not `has_lifecycle_or_approval`|

Out of scope (for now): 5.2.4 Multidimensional objects, 5.2.18 EXPRESS types,
5.2.25 Set operations, 5.2.29 Shapes.

## Nature-scan question table (prompt #1)

| Question key | English | Triggers prompt(s) |
|---|---|---|
| `describes_activities` | does it describe processes, events, procedures, work performed, or things that happen over time? | 2, 7 |
| `describes_individuals` | does it name specific persons, organizations, or physical objects (not generic types)? | 3 |
| `defines_classes` | does it define categories, types, or a taxonomy (rather than only describe instances)? | 4, 5 |
| `describes_roles` | does it state who does what, or which role an entity plays in some activity? | 6, 7 |
| `has_temporal_structure` | does it specify dates, durations, sequences, or before/after ordering of events? | 9 |
| `describes_whole_parts` | does it describe compositional structure (assemblies, sub-systems, sub-procedures, document sections)? | 8 |
| `has_properties` | does it list qualities or attributes of things (color, function, status, …)? | 10 |
| `has_quantities` | does it state numeric values with units (50 kg, 3 bar, 12 V)? | 11 |
| `has_identifiers` | does it use codes, IDs, tag numbers, or labels to name things (P-101, ISO 9001, PO-2024-447)? | 12 |
| `describes_connections` | does it describe physical or logical connectivity (pipe X connects to vessel Y, system A feeds system B)? | 13 |
| `has_lifecycle_or_approval` | does it record status changes, approvals, revisions, lifecycle stages, or sign-offs? | 14 |

## Coverage metrics

After prompt #1, two cheap deterministic metrics are computed in Python and
stored on the extraction activity:

- **Evidence coverage** — Σ chars of evidence quotes across all `yes`
  answers / total document chars. "How dense is this doc with extractable
  content?"
- **Scope coverage** — `yes_count / 11`. "How many content axes does this
  doc span?"

Both are reported in the result struct; neither costs extra LLM tokens.

## Output format

Every prompt returns a single JSON object, no prose, no fences. The Python
converter assembles the JSON outputs into Turtle in the named extraction
graph. Reification of Part 2 idioms (Classification, Participation,
CompositionOfIndividual, Property+Scale, …) is generated by the converter,
not the LLM.

## Verified Part 2 property names

Confirmed by inspecting `docs/ISO-15926-2_2003.rdf`. All converter mappings
in the prompt files use these names; placeholders in earlier drafts have
been superseded.

| Class (domain) | Property | Range |
|---|---|---|
| `Approval` | `hasApprover`, `hasApproved` | participants |
| `Classification` | `hasClassifier`, `hasClassified` | classifier / classified |
| `CauseOfEvent` | `hasCauser`, `hasCaused` | cause / effect events |
| `CompositionOfIndividual` (and subclasses) | `hasWhole`, `hasPart` | the two endpoints |
| `IndividualUsedInConnection` | `hasUsage`, `hasConnection` | connecting individual / connection |
| `LifecycleStage` | `hasInterested`, `hasInterest` | bearer / stage |
| `Specialization` | `hasSubclass`, `hasSuperclass` | sub / super class |
| `TemporalSequence` | `hasPredecessor`, `hasSuccessor` | earlier / later |

**Structural quirk**: `Participation rdfs:subClassOf CompositionOfIndividual`.
A participation IS-A composition. The activity is the *whole*; the
participant is the *part*. Prompt #7's converter uses `hasWhole` /
`hasPart`, not invented names.

Several classes have no directly-domained properties and inherit them
through the subclass chain (Activity, Property, Identification, role
classes, connection subclasses). The converter walks the chain at
implementation time and uses the inherited names.

## Decision log

- **Strict Part 2 source-typing** (no `dg:InformationObject` shortcut). 2026-04-29.
- **Connections (5.2.21) and Lifecycle/Approvals (5.2.23) are first-class
  prompts**, not folded into others. 2026-04-29.
- **JSON-then-convert** (LLM does not emit Turtle). Cost reasons. 2026-04-29.
- **Activity and Event live in one prompt** (#2). One Part 2 section. 2026-04-29.
- **Cross-document parents inferred from `iso_class`**, not requested
  from the LLM. 2026-04-29.
- **Aliases preserved as a separate JSON field**, not folded into label.
  Useful for downstream URI resolution. 2026-04-29.
- **`location` and `stream` are first-class kinds** in prompt #3, not
  folded into `physical_object`. They have dedicated Part 2 individual
  classes. 2026-04-29.
- **TBD:** in prompt #4, whether `definition` should be paraphrased (cleaner
  for `rdfs:comment`) or verbatim (preserves fidelity). Currently
  paraphrased + verbatim `evidence`. 2026-04-29.
- **Material subdivisions folded** into one `material` kind in prompt #5.
  Default Part 2 mapping is `ClassOfCompositeMaterial`. 2026-04-29.
- **Chemistry-specific classes dropped** (atoms, molecules, sub-atomic
  particles) — out of scope. 2026-04-29.
- **Ad-hoc `doc_kind` class** minted as `ClassOfInformationObject` and
  stored in the same named graph as the extracted entities (cascades on
  document removal). 2026-04-29.
- **Roles modelled as `ClassOfPossibleRoleAndDomain`** in prompt #6.
  Part 2 has no standalone `Role` class — every role is a relationship
  between a kind of player and a kind of activity. Domain and player are
  optional and constrained to already-extracted classes. 2026-04-29.
- **Participations** (#7) emit `Participation` objects; when a role is
  set, an additional `IntendedRoleAndDomain` is reified. No-role
  participations are kept. 2026-04-29.
- **Whole-parts** (#8) allow new individuals/activities to be introduced
  via `new_individuals` / `new_activities`. Five `relation_kind` values:
  spatial, temporal, feature, informational, other. 2026-04-29.
- **Temporal** (#9) — seven relation_kinds. `after` is normalised to
  `before` by swapping. `triggers` dropped (Part 2 has only
  `CauseOfEvent`). 2026-04-29.
- **Properties** (#10) split qualitative (here) from quantitative
  (#11). Bearers can be individuals, activities, or classes. Same
  `property_kind` string → one shared `ClassOfProperty` URI.
  Approval-style properties are captured both here and in #14;
  converter deduplicates. 2026-04-29.
- **Quantities** (#11) flat three-field shape (`exact`/`min`/`max`).
  Strict inequality dropped. Unit normalisation in the converter.
  2026-04-29.
- **Identifiers & descriptions** (#12) skip only when the document has
  no identifiers AND defines no classes AND describes no individuals —
  in practice runs on every non-trivial doc. Cross-references kept as
  their own representation_kind. 2026-04-29.
- **Connections** (#13) allow `new_individuals` (pipes/cables often
  mentioned only in passing). `nature` is free-form snake_case with
  suggested values. `direction` carried on `dg:direction` literal —
  Part 2 doesn't model it at the connection level. 2026-04-29.
- **Lifecycle & approvals** (#14) — three parallel lists (approvals,
  lifecycle_stages, revisions). Revision modelled as `Identification` +
  `dg:supersedes` (no Part 2 revision class). `by` field scoped to
  approvals only. 2026-04-29.
