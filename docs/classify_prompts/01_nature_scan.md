# Prompt #1 — Document nature scan

**Purpose**: classify what kinds of content the document contains, so only
relevant downstream prompts run.

**Skip condition**: never — always runs.

**Inputs**: cached document markdown.

**Outputs (gates)**: 11 yes/no answers + `doc_kind` + `primary_subjects`.

## Prompt body

```
You are scanning an industrial / business document to decide what kinds of
content it contains. Your output gates a downstream extraction pipeline
based on ISO 15926-2.

Read the document and answer eleven yes/no questions. For each "yes", give a
one-line evidence quote (verbatim) from the document. For each "no", leave
evidence empty. Do not classify content you only weakly suspect — only
mark "yes" when you can quote a concrete supporting line.

Then give:
- doc_kind: a short noun phrase naming the document type (e.g. "purchase
  order", "process safety standard", "P&ID drawing", "annual report",
  "maintenance procedure", "personnel record"). Free-form, ~5 words max.
- primary_subjects: 1-3 noun phrases naming what the document is mainly
  about (e.g. ["centrifugal pump P-101", "lubrication maintenance"]).

The eleven questions:

  Q1  describes_activities      — does it describe processes, events,
                                   procedures, work performed, or things
                                   that happen over time?
  Q2  describes_individuals     — does it name specific persons,
                                   organizations, or physical objects
                                   (not generic types)?
  Q3  defines_classes           — does it define categories, types, or a
                                   taxonomy (rather than only describe
                                   instances)?
  Q4  describes_roles           — does it state who does what, or which
                                   role an entity plays in some activity?
  Q5  has_temporal_structure    — does it specify dates, durations,
                                   sequences, or before/after ordering of
                                   events?
  Q6  describes_whole_parts     — does it describe compositional structure
                                   (assemblies, sub-systems, sub-procedures,
                                   document sections)?
  Q7  has_properties            — does it list qualities or attributes of
                                   things (color, function, status, …)?
  Q8  has_quantities            — does it state numeric values with units
                                   (50 kg, 3 bar, 12 V)?
  Q9  has_identifiers           — does it use codes, IDs, tag numbers, or
                                   labels to name things (P-101, ISO 9001,
                                   PO-2024-447)?
  Q10 describes_connections     — does it describe physical or logical
                                   connectivity (pipe X connects to vessel
                                   Y, system A feeds system B)?
  Q11 has_lifecycle_or_approval — does it record status changes, approvals,
                                   revisions, lifecycle stages, or sign-offs?

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "doc_kind": "...",
  "primary_subjects": ["...", "..."],
  "answers": {
    "describes_activities":      {"yes": true|false, "evidence": "..."},
    "describes_individuals":     {"yes": true|false, "evidence": "..."},
    "defines_classes":           {"yes": true|false, "evidence": "..."},
    "describes_roles":           {"yes": true|false, "evidence": "..."},
    "has_temporal_structure":    {"yes": true|false, "evidence": "..."},
    "describes_whole_parts":     {"yes": true|false, "evidence": "..."},
    "has_properties":            {"yes": true|false, "evidence": "..."},
    "has_quantities":            {"yes": true|false, "evidence": "..."},
    "has_identifiers":           {"yes": true|false, "evidence": "..."},
    "describes_connections":     {"yes": true|false, "evidence": "..."},
    "has_lifecycle_or_approval": {"yes": true|false, "evidence": "..."}
  }
}
```

## Gating logic (Python, post-prompt)

| Question yes | Triggers prompt(s) |
|---|---|
| `describes_activities` | 2, 7 |
| `describes_individuals` | 3 |
| `defines_classes` | 4, 5 |
| `describes_roles` | 6, 7 |
| `has_temporal_structure` | 9 |
| `describes_whole_parts` | 8 |
| `has_properties` | 10 |
| `has_quantities` | 11 |
| `has_identifiers` | 12 |
| `describes_connections` | 13 |
| `has_lifecycle_or_approval` | 14 |

`doc_kind` becomes a `ClassOfInformationObject` candidate (the source
document gets typed as `iso15926:WholeLifeIndividual` plus an ad-hoc
subclass derived from `doc_kind`).

`primary_subjects` becomes URI seeds for prompt #3 (Individuals).

## Coverage metrics (computed in Python)

- **Evidence coverage** = Σ chars of evidence quotes across all `yes`
  answers / total document chars.
- **Scope coverage** = yes_count / 11.

Both stored on the extraction-activity node alongside `dg:confidence`.

## Decisions

- 11 questions, including Q11 lifecycle/approval. 2026-04-29.
- Evidence quotes kept (debugging + provenance value > token cost). 2026-04-29.
- Both coverage metrics reported. 2026-04-29.
