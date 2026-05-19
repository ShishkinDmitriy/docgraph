# Prompt #9 — Temporal relationships

**Purpose**: extract temporal ordering and causal relationships between
activities/events — A happens before B, A causes B, A is concurrent with B.
Distinct from prompt #8 (part-of); this prompt is *ordering*.

**Skip condition**: prompt #1 says `has_temporal_structure` is `false`.

**Part 2 §**: 5.2.22 Relative locations and sequences
(`temporal_sequence`); 5.2.9 (`cause_of_event`).

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` +
activities/events table from #2 (extended with any `new_activities` from
#8).

**Outputs**: list of temporal links between two activities or events.

## Prompt body

```
You are extracting temporal and causal relationships between activities
or events, mapping to ISO 15926-2.

For each relationship, identify the two activities/events involved and
the kind of temporal connection. The kinds:

- "before"     — A finished before B started.
                 ("inspection was completed before commissioning")
- "after"      — A started after B ended (mirror of before).
- "during"     — A happened entirely within the period of B.
                 ("commissioning happened during the shutdown")
- "overlaps"   — A and B partly overlap in time.
- "concurrent" — A and B happen at the same time, or for the same
                 stretch.
- "causes"     — A causes B (causal, not just earlier). Use this for
                 both ordinary causation AND immediate triggering;
                 ISO 15926-2 has only one causal class.
- "follows"    — A follows B in a defined sequence (e.g. step 3
                 follows step 2 in a procedure), without strong
                 timestamp evidence.

For each relationship:
- id:            short slug (lowercase, hyphenated, unique within this doc)
- earlier:       id of the activity/event that is earlier (or causal
                 antecedent). For "concurrent" and "overlaps", pick
                 either deterministically (alphabetical by id).
- later:         id of the other activity/event
- relation_kind: one of the kinds above
- description:   one short phrase, or ""
- evidence:      verbatim quote from the document

Use ids exactly as given in the lists. Do not invent activity ids.
If an ordering involves an entity not in the activity list, omit it.

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted activities and events:
{activity_id_label_summary_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "temporal_relations": [
    {
      "id":            "...",
      "earlier":       "...",
      "later":         "...",
      "relation_kind": "before" | "after" | "during" | "overlaps" |
                       "concurrent" | "causes" | "follows",
      "description":   "...",
      "evidence":      "..."
    }
  ]
}

If no temporal/causal relationships are described, return
{"temporal_relations": []}.
```

## Converter mapping

| `relation_kind` | Part 2 reification |
|---|---|
| before / after / follows | `TemporalSequence` (with earlier/later properties; `after` normalised by swapping) |
| during / overlaps / concurrent | `TemporalSequence` + `dg:overlap` qualifier |
| causes | `CauseOfEvent` |

```turtle
ext:t-001  a iso15926:TemporalSequence ;
    iso15926:hasEarlier ext:inspection-2024 ;
    iso15926:hasLater   ext:commissioning-2024 ;
    dg:summary  "Inspection completed before commissioning." ;
    dg:evidence "Inspection was completed before commissioning began." .

ext:t-002  a iso15926:CauseOfEvent ;
    iso15926:hasCause  ext:pressure-spike ;
    iso15926:hasEffect ext:v-12-open-event ;
    dg:evidence "The pressure spike caused valve V-12 to open." .
```

Exact property names verified against the OWL when implementing the converter.

## Decisions

- Seven `relation_kind` values. `after` kept for LLM convenience and
  normalised to `before` (swapping earlier/later) by the converter.
  2026-04-29.
- `triggers` dropped — Part 2 only has `CauseOfEvent`, so both ordinary
  and immediate causation share one relation_kind. 2026-04-29.
- `new_activities` from prompt #8 fold into the activity list passed
  here. 2026-04-29.
