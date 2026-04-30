# Prompt #8 — Whole-parts

**Purpose**: extract compositional structure — physical part-of, sub-activity,
feature-of, and document-section relationships. Captures both spatial
composition (assemblies, parts) and temporal composition (sub-activities).

**Skip condition**: prompt #1 says `describes_whole_parts` is `false`.

**Part 2 §**: 5.2.6 §4.7.1–2 — `composition_of_individual`,
`temporal_whole_part`, `feature_whole_part`, `assembly_of_individual`,
`arrangement_of_individual`.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + activities
table from #2 + individuals table from #3.

**Outputs**: list of whole-part links + (when needed) new individuals or
activities introduced as parts.

## Prompt body

```
You are extracting whole-part (compositional) relationships from a
document, mapping to ISO 15926-2.

A whole-part relationship has one whole and one part. The kinds we care
about:

- "spatial"      — a physical thing has another physical thing as a part
                   (assembly, sub-system, contained component)
                   e.g. "Pump P-101 contains an impeller and a casing"
- "temporal"     — an activity has a sub-activity as a part
                   e.g. "the audit consisted of a planning phase and a
                   fieldwork phase"
- "feature"      — a thing has a structural or geometric feature
                   (a flange on a vessel, a hole in a plate)
- "informational"— a document section is part of a document
                   e.g. "Annex A is part of the standard"
- "other"        — explain in `note`

For each relationship:
- id:            short slug (lowercase, hyphenated, unique within this doc)
- whole:         id of an already-extracted entity (activity or
                 individual) that is the whole
- part:          id of the part. May be:
                 (a) an id from the already-extracted lists, OR
                 (b) a NEW slug. If new, you must also add an entry to
                     `new_individuals` (or `new_activities`) with the same
                     id.
- relation_kind: one of the kinds above
- description:   one short phrase, or ""
- evidence:      verbatim quote from the document
- note:          free-text only when needed (e.g. for "other")

If you introduce a new part, add it to one of:

- `new_individuals`: with id / label / kind (the same kinds as in the
  Individuals prompt: person, organization, physical_object,
  functional_object, location, stream, other) / evidence.
- `new_activities`: with id / label / iso_class (Activity or Event) /
  summary / evidence.

Only introduce new entities when they are clearly described in the
document but were missed by earlier extraction. If a part is mentioned
only in passing without enough context to type, omit the relationship.

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted activities:
{activity_id_label_summary_table}
- already-extracted individuals:
{individual_id_label_kind_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "whole_parts": [
    {
      "id":            "...",
      "whole":         "...",
      "part":          "...",
      "relation_kind": "spatial" | "temporal" | "feature" |
                       "informational" | "other",
      "description":   "...",
      "evidence":      "...",
      "note":          ""
    }
  ],
  "new_individuals": [
    {
      "id":       "...",
      "label":    "...",
      "kind":     "person" | "organization" | "physical_object" |
                  "functional_object" | "location" | "stream" | "other",
      "evidence": "..."
    }
  ],
  "new_activities": [
    {
      "id":        "...",
      "label":     "...",
      "iso_class": "Activity" | "Event",
      "summary":   "...",
      "evidence":  "..."
    }
  ]
}

If no whole-part relationships are described, return all three lists empty.
```

## Converter mapping

| `relation_kind` | Part 2 reification class |
|---|---|
| spatial | `CompositionOfIndividual` (or `AssemblyOfIndividual` if hinted) |
| temporal | `TemporalWholePart` |
| feature | `FeatureWholePart` |
| informational | `CompositionOfIndividual` + `dg:note "informational"` |
| other | `CompositionOfIndividual` + `dg:status dg:Unresolved` |

```turtle
ext:wp-001  a iso15926:CompositionOfIndividual ;
    iso15926:hasWhole ext:p-101 ;
    iso15926:hasPart  ext:impeller-of-p101 ;
    dg:summary  "P-101 contains an impeller." ;
    dg:evidence "Pump P-101 contains an impeller and a casing." .
```

`new_individuals` and `new_activities` flow through the same converter
routines as prompts #3 and #2 respectively before being linked.

## Decisions

- Approach B: allow new parts to be introduced via `new_individuals` /
  `new_activities`. Richer than only-link-existing, modest schema cost.
  2026-04-29.
- Five `relation_kind` values, covering Part 2's main whole-part classes
  plus an "informational" bucket for document sections. 2026-04-29.
- Sub-activities (temporal whole-part) live here. Prompt #9 covers
  ordering only (before/after). 2026-04-29.
