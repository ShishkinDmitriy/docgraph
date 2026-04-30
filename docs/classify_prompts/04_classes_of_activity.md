# Prompt #4 — Classes of activity

**Purpose**: extract type-level definitions of activities and events that
the document explicitly defines. Distinct from prompt #2 (which extracts
specific instances).

**Skip condition**: prompt #1 says BOTH `defines_classes` is `false` AND
`describes_activities` is `false`.

**Part 2 §**: 5.2.10 Classes of activity.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + the
activity-instance table from prompt #2 (so the LLM can link instances to
the classes being defined here).

**Outputs**: list of class definitions with parent / instances / evidence.

## Prompt body

```
You are extracting class-level (type) definitions of activities and events
from a document, mapping to ISO 15926-2.

In ISO 15926-2:
- ClassOfActivity      — a kind of activity (e.g. "Maintenance",
                         "Internal Audit", "Pressure Test").
- ClassOfEvent         — a kind of event   (e.g. "Valve Opening",
                         "Contract Signature").
- ClassOfPeriodInTime  — a kind of period  (e.g. "Fiscal Quarter",
                         "Shutdown Window").
- ClassOfPointInTime   — a kind of moment  (e.g. "Commissioning Date").

A class definition is when the document explicitly DEFINES or DESCRIBES
a category — not when it merely mentions one in passing.

Examples that DEFINE a class:
- "An internal audit is a systematic, independent review of internal
   processes performed by employees of the same organization."
- "A pressure test consists of subjecting a vessel to 1.5x its design
   pressure for a period of at least 30 minutes."

Examples that do NOT define a class (just mention one):
- "We performed three audits last quarter."
- "The pump failed during a pressure test."
  (Mentioning the test, not defining what one is.)

For each class definition:
- id:          short slug (lowercase, hyphenated, unique within this doc)
- label:       short human-readable name
- iso_class:   one of "ClassOfActivity", "ClassOfEvent",
               "ClassOfPeriodInTime", "ClassOfPointInTime"
- definition:  the document's own definition, paraphrased to one or two
               sentences (no quotes; clean prose)
- parent:      id of a parent class defined elsewhere in this prompt's
               output, or null. Use this when one class is a subtype of
               another defined here ("an internal audit is a kind of
               audit").
- instances:   ids of already-extracted activities (from prompt #2) that
               are instances of this class. May be []. Use the activity
               ids exactly as given.
- evidence:    verbatim quote from the document

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted activity instances:
{activity_id_label_summary_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "classes_of_activity": [
    {
      "id":          "...",
      "label":       "...",
      "iso_class":   "ClassOfActivity" | "ClassOfEvent" |
                     "ClassOfPeriodInTime" | "ClassOfPointInTime",
      "definition":  "...",
      "parent":      "..." | null,
      "instances":   ["...", "..."],
      "evidence":    "..."
    }
  ]
}

If no class-level definitions are found, return
{"classes_of_activity": []}.
```

## Converter mapping

```turtle
# Class definition
ext:internal-audit  a iso15926:ClassOfActivity ;
    rdfs:label    "Internal Audit" ;
    rdfs:comment  "A systematic, independent review of internal processes…" ;
    rdfs:subClassOf ext:audit ;     # if parent set
    dg:evidence   "An internal audit is a systematic…" .

# Instance link via Part 2 reified Classification
ext:c-001  a iso15926:Classification ;
    iso15926:hasClassifier ext:internal-audit ;
    iso15926:hasClassified ext:audit-2024-q1 .
```

## Decisions

- Instance-to-class linking happens in this prompt (LLM sees instance list
  and correlates) rather than a separate linking pass. 2026-04-29.
- `parent` only refers to classes defined within this prompt's output;
  cross-document parents are inferred from `iso_class` by the converter.
  2026-04-29.
- **TBD:** `definition` paraphrased vs verbatim. Currently paraphrased,
  with `evidence` carrying the verbatim quote. 2026-04-29.
