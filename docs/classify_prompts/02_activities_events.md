# Prompt #2 — Activities & events

**Purpose**: extract every activity / event / process / procedure described
in the document, plus their temporal bounds where stated. Reification of
participations and class-of-activity comes in later prompts.

**Skip condition**: prompt #1 says `describes_activities` is `false`.

**Part 2 §**: 5.2.9 Activities and events.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` from prompt #1.

**Outputs**: list of activity / event entries with begin / end strings.

## Prompt body

```
You are extracting activities and events from a document, mapping them
to ISO 15926-2 classes.

In ISO 15926-2:
- An Activity is something that happens over a period and changes
  something (e.g. "lubricate pump P-101", "issue invoice 4471",
  "perform safety audit").
- An Event is a happening at a point in time, with no duration
  (e.g. "valve V-12 opened at 14:30", "contract signed").
- A PointInTime is a specific moment (e.g. "2024-03-15", "14:30 UTC").
- A PeriodInTime is a stretch of time (e.g. "Q1 2024",
  "shutdown window").
- A Beginning / Ending bound an activity (e.g. "the audit
  started 2024-03-15").

Extract every distinct activity or event the document describes.
Do NOT extract the document's own creation as an activity unless the
document is explicitly about that creation. Do NOT extract activities
that are only mentioned as type-definitions ("an audit is a kind of
review"); those go to a later prompt about classes of activity.

For each activity/event, give:
- id:        short slug (lowercase, hyphenated, unique within this doc)
- label:     short human-readable name
- iso_class: one of "Activity", "Event"
- summary:   one sentence describing what happens
- begin:     ISO-8601 datetime, or natural-language phrase, or null
- end:       ISO-8601 datetime, or natural-language phrase, or null
- evidence:  verbatim quote from the document supporting this entry

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "activities": [
    {
      "id": "...",
      "label": "...",
      "iso_class": "Activity" | "Event",
      "summary": "...",
      "begin": "..." | null,
      "end":   "..." | null,
      "evidence": "..."
    }
  ]
}

If no activities or events are described, return {"activities": []}.
```

## Converter mapping (illustrative)

```turtle
ext:act-lubricate-p101  a iso15926:Activity ;
    rdfs:label "Lubricate pump P-101" ;
    dg:summary "Quarterly lubrication of centrifugal pump P-101." ;
    iso15926:hasBeginning [
        a iso15926:Beginning ;
        iso15926:atTime "2024-03-15"^^xsd:date
    ] ;
    dg:evidence "Pump P-101 was lubricated on 15 March 2024." .
```

`PointInTime` / `PeriodInTime` / `Beginning` / `Ending` reification happens
in code, not the LLM. Natural-language begin/end strings that don't parse
to ISO-8601 are preserved as `xsd:string` literals and tagged
`dg:status dg:Unresolved`.

## Decisions

- Activity and Event share one prompt (same Part 2 section). 2026-04-29.
- `begin` / `end` accept either ISO-8601 or natural language; the converter
  handles both. 2026-04-29.
- Cause/effect relations deferred to prompt #9 (Temporal). 2026-04-29.
