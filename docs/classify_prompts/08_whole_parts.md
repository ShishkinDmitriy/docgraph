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

## Future patterns — temporal slices of whole-life individuals

Today P08 emits `TemporalWholePart` only for **sub-activity composition**
(activity-of-activity). Part 2's `temporal_whole_part` (§5.2.6.14) also
covers **object-during-period** — e.g. *"Pump P-101 during Q3 2024"*.
Three spec-compliant patterns for that case, in increasing weight; pick
the lightest one that captures what the document says.

**Vocabulary**:
- *whole-life individual* — the entity across all time (e.g. `:p101`,
  emitted by P03 with `iso15926:WholeLifeIndividual` on the perspective
  axis).
- *slice* — a `possible_individual` of the **same kind** as the whole
  (e.g. still a `FunctionalPhysicalObject`) but **without**
  `WholeLifeIndividual` — Part 2 §5.2.6.14: *"being a whole_life_individual
  is not inherited by its temporal parts"*.
- *event / point_in_time* — a zero-extent possible_individual marking the
  start or end (e.g. `:evt_2024_07_01`, classified as `iso15926:PointInTime`).

`TemporalWholePart` itself only carries `hasWhole` + `hasPart`. The
"which period" answer lives on a separate node attached to the slice or
the link.

### Way 1 — Bound the slice with `Beginning` / `Ending`

The canonical Part 2 pattern. Reuses the same Beginning/Ending shape
P02 already emits for activities. Tells you *when* the slice exists by
its boundary events; does not link to a named period.

```turtle
:p101_during_q3   a iso15926:ActualIndividual,
                    iso15926:FunctionalPhysicalObject,
                    ext:cls/centrifugal-pump .
                  # NO WholeLifeIndividual — it is a slice

:tp_p101_q3       a iso15926:TemporalWholePart ;
                  iso15926:hasWhole :p101 ;
                  iso15926:hasPart  :p101_during_q3 .

:beg_q3           a iso15926:Beginning ;            # the relation, not the moment
                  iso15926:hasWhole :p101_during_q3 ;
                  iso15926:hasPart  :evt_2024_07_01 .

:evt_2024_07_01   a iso15926:PointInTime ;          # the moment
                  iso15926:hasContent "2024-07-01"^^xsd:date .
```

Trigger: documents that describe an asset with explicit start/end dates
but don't name the period (condition reports, as-was snapshots).

### Way 2 — Slice participates in an activity

Per §5.2.9.1, *"an activity consists of the temporal parts of those
members of possible_individual that participate in the activity"*. The
slice's temporal extent is inherited from the activity's bounds — no
separate Beginning/Ending on the slice.

```turtle
:operating_q3   a iso15926:Activity .
:beg_op         a iso15926:Beginning ;  iso15926:hasWhole :operating_q3 ;
                                        iso15926:hasPart :evt_2024_07_01 .
:end_op         a iso15926:Ending ;     iso15926:hasWhole :operating_q3 ;
                                        iso15926:hasPart :evt_2024_09_30 .

:p101_during_q3 a iso15926:ActualIndividual,
                  iso15926:FunctionalPhysicalObject,
                  ext:cls/centrifugal-pump .

:tp             a iso15926:TemporalWholePart ;
                iso15926:hasWhole :p101 ;
                iso15926:hasPart  :p101_during_q3 .

:part_in_op     a iso15926:Participation ;
                iso15926:hasWhole :operating_q3 ;
                iso15926:hasPart  :p101_during_q3 .
```

Trigger: documents where the period is defined by a named activity (an
operating period, a maintenance window, a project phase). **Note**:
today's `participations.py` shortcuts to use the whole-life URI as the
participant; spec-strict would use the slice URI here.

### Way 3 — Classify the link by a `ClassOfTemporalWholePart` keyed to a `period_in_time`

Heaviest. Used when the period is itself a named, reusable thing across
multiple documents ("Q3 2024" as a fiscal-period concept, used to
co-classify many slices).

```turtle
:period_q3_2024            a iso15926:PeriodInTime ;
                           rdfs:label "Q3 2024" .

:cls_pump_during_q3_2024   a iso15926:ClassOfTemporalWholePart ;
                           rdfs:label "operating-period-Q3-2024-of-pumps" .
                           # class-level role/domain links it to :period_q3_2024

:tp_p101_q3                a iso15926:TemporalWholePart ;
                           a :cls_pump_during_q3_2024 ;
                           iso15926:hasWhole :p101 ;
                           iso15926:hasPart  :p101_during_q3 .
```

Trigger: "show me every asset during Q3 2024" cross-document queries.
Out of scope until a real corpus needs it.

### What needs to change to implement these

| Step | Files | Effort |
|---|---|---|
| Recognize "object during period" in P08 | `08_whole_parts.md` prompt body | small |
| Add slice perspective to P03 (or P08 `new_individuals`) | `convert/individuals.py` (suppress WholeLifeIndividual when `perspective="slice"`) | small |
| Way 1 — emit Beginning/Ending on a slice | factor `_attach_temporal_bound` out of `convert/activities.py`, call from `convert/whole_parts.py` | medium |
| Way 2 — participations link slices, not whole-life URIs | `convert/participations.py` | medium (semantic change) |
| Way 3 — `ClassOfTemporalWholePart` minting | new converter helper | medium-large |

Defer all of the above until a real document exercises one of the
trigger cases. Way 1 first.

## Decisions

- Approach B: allow new parts to be introduced via `new_individuals` /
  `new_activities`. Richer than only-link-existing, modest schema cost.
  2026-04-29.
- Five `relation_kind` values, covering Part 2's main whole-part classes
  plus an "informational" bucket for document sections. 2026-04-29.
- Sub-activities (temporal whole-part) live here. Prompt #9 covers
  ordering only (before/after). 2026-04-29.
- Object-during-period (asset-slice) cases captured in "Future patterns"
  above. Defer implementation until a concrete document needs it; Way 1
  (Beginning/Ending on the slice) is the smallest spec-aligned change.
  2026-05-03.
