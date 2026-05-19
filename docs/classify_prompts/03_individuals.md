# Prompt #3 — Individuals

**Purpose**: extract every named, specific entity in the document — persons,
organizations, physical objects, locations, streams. Generic types are
excluded; only named instances qualify.

**Skip condition**: prompt #1 says `describes_individuals` is `false`.

**Part 2 §**: 5.2.6 Possible individuals; 5.2.7 Classes of individual.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + the
activity-id table emitted by prompt #2 (cross-reference only).

**Outputs**: list of individual entries with kind / aliases / evidence.

## Prompt body

```
You are extracting named individuals from a document. An individual is a
specific, identifiable thing — not a category or type.

Examples that ARE individuals:
- "Pump P-101"               — a specific pump
- "John Smith"               — a specific person
- "ACME Corporation"         — a specific organization
- "the Forties pipeline"     — a specific physical object
- "Building 4, Floor 2"      — a specific location

Examples that are NOT individuals (they are classes — extracted later):
- "centrifugal pumps"        — a kind of pump
- "managers"                 — a role/kind of person
- "ISO 9001 standards"       — a class of document

Categorize each individual into ONE of these kinds:
- "person"           — a named human being
- "organization"     — a company, agency, team, association
- "physical_object"  — a tangible thing (equipment, vehicle, material)
- "functional_object"— equipment defined by what it does
                       (e.g. "the level controller", "Pump P-101")
- "location"         — a place or spatial reference
- "stream"           — a flow of material or information
                       (e.g. "the feed stream to V-101")
- "other"            — none of the above; explain in `note`

For each individual:
- id:        short slug (lowercase, hyphenated, unique within this doc)
- label:     short human-readable name as it appears in the document
- kind:      one of the categories above
- existence: "actual" or "possible" — see below; default "actual"
- aliases:   other names the document uses for the same individual ([] if none)
- summary:   one-sentence description (or "" if none warranted)
- evidence:  verbatim quote
- note:      free-text only when needed (e.g. for "other")

`existence` distinguishes things the document describes as **real, in-service,
observed, or already-happened** (`"actual"`) from things described as
**planned, designed, proposed, specified-but-not-yet-built, requested,
or hypothetical** (`"possible"`).

Default `"actual"`. Choose `"possible"` only when the document is clearly
forward-looking or hypothetical about *this specific* individual.

Examples — `"actual"`:
- "Pump P-101 was inspected on 2024-03-15"          (already happened)
- "ACME Corporation"                                 (a real org)
- "John Smith signed the purchase order"             (a person who acted)
- "the existing feed line"                           (in-service)

Examples — `"possible"`:
- "the proposed Phase-2 expansion will include …"    (planned)
- "P-201 (TBD): replacement for P-101"               (designed, not built)
- "this specification covers a future control valve" (forward-looking)
- "in the design, V-103 is sized for 50 m³/h"        (hypothetical / on paper)

If the document mixes — e.g. "Pump P-101 (existing) and Pump P-201 (planned
2026 retrofit)" — emit two entries with different `existence` values.

Deduplicate: if "P-101" and "pump P-101" refer to the same thing, emit one
entry with both names in `label`/`aliases`.

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted activities: {activity_ids_and_labels}  # for cross-reference only

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "individuals": [
    {
      "id": "...",
      "label": "...",
      "kind": "person" | "organization" | "physical_object" |
              "functional_object" | "location" | "stream" | "other",
      "existence": "actual" | "possible",
      "aliases": ["..."],
      "summary": "...",
      "evidence": "...",
      "note": ""
    }
  ],
  "locations_of": [
    {"individual": "<id>", "location": "<id>"}
  ]
}

`locations_of` connects a non-location individual (a person, organization,
physical_object, functional_object) to one of the location individuals
also extracted in this same call. Use it whenever the document associates
the individual with an address or place — e.g. a person with a home
address, an organization with a business address, a piece of equipment
with a building or room. Reference ids exactly as given in the
`individuals` list. Leave the list empty if no location-of relationships
are stated.

If no named individuals are described, return {"individuals": [], "locations_of": []}.
```

## Converter mapping

Every individual is typed by **a stack** of classes, not just one:

1. **Modal axis** — chosen by `existence`:
   - `"actual"`   → `iso15926:ActualIndividual`
   - `"possible"` → `iso15926:PossibleIndividual`
2. **Perspective axis** — always `iso15926:WholeLifeIndividual` (P03 has no
   time-slice semantics; every extracted individual is the whole-life view).
3. **Kind axis** — chosen by `kind` (the table below).
4. **Specific class** — the minted `ClassOf*` subclass (column 3).

When two axes pick the same class (e.g. `kind="person"` whose kind class is
also `WholeLifeIndividual`), it is emitted only once.

| `kind` | Part 2 individual class | Default classifier (ClassOf*) |
|---|---|---|
| person | `WholeLifeIndividual` | `ClassOfPerson` |
| organization | `WholeLifeIndividual` | `ClassOfOrganization` |
| physical_object | `PhysicalObject` | `ClassOfInanimatePhysicalObject` |
| functional_object | `FunctionalPhysicalObject` | `ClassOfFunctionalObject` |
| location | `SpatialLocation` | (none unless prompt #5 supplies one) |
| stream | `Stream` | (none unless prompt #5 supplies one) |
| other | (none — only the modal class above) | `dg:status dg:Unresolved` |

When the kind class would equal the modal class (the legacy `kind="other"`
fallback), the converter dedupes — only the modal triple is emitted in
that case.

**Specific** classes (e.g. "centrifugal pump") come from prompt #5; this
prompt only attaches the broad kind.

Aliases become `skos:altLabel` triples on the individual.

## Decisions

- Strict Part 2 source-document typing: `WholeLifeIndividual` +
  `ClassOfInformationObject` subclass derived from `doc_kind`. No
  `dg:InformationObject` shortcut. 2026-04-29.
- `aliases` kept as a separate JSON field (helps downstream URI
  resolution). 2026-04-29.
- `location` and `stream` are first-class kinds (Part 2 has dedicated
  individual classes). 2026-04-29.
