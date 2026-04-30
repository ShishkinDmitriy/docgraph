# Prompt #12 — Identifiers & descriptions

**Purpose**: extract codes, IDs, tag numbers, formal names,
cross-references, and document-attached definitions/descriptions —
anything Part 2 calls a "representation of a thing".

**Skip condition**: prompt #1 says ALL THREE of `has_identifiers`,
`defines_classes`, `describes_individuals` are `false`. In practice this
almost never skips.

**Part 2 §**: 5.2.16 Representations of things.

**Note on overlap with earlier prompts**: prompt #3 already captured
`summary` for individuals and `aliases`; prompts #4/#5 already captured
`definition` for classes. This prompt is for *additional* formal
representations Part 2 cares about: identifiers with naming systems,
cross-references to other documents, and definitions/descriptions whose
source needs to be reified.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + every
previously extracted entity (individuals, activities,
classes-of-individual, classes-of-activity, roles).

**Outputs**: list of representation entries.

## Prompt body

```
You are extracting representations of things — identifiers, codes, formal
names, cross-references, definitions, and descriptions — from a document,
mapping to ISO 15926-2.

A representation is a piece of information that names, identifies,
defines, or describes some thing. The kinds we care about:

- "identifier"     — a code or tag number assigned to a thing
                     (e.g. "P-101", "ISO 9001:2015", "PO-2024-447",
                     "EN-12345", part numbers, serial numbers).
- "name"           — a formal proper name as opposed to a code
                     (e.g. company name "ACME Corporation Ltd.",
                     project name "Forties Phase 3").
- "alias"          — an alternate name not already captured as label or
                     primary identifier.
- "description"    — a textual description of a thing, where the
                     document or one of its sections is itself the
                     source of the description.
- "definition"     — a formal definition stated in the document, where
                     it matters who/where defined it.
- "cross_reference"— a pointer to another document that defines or
                     describes the thing
                     (e.g. "as specified in EN 13480-3", "see Annex B").

Do NOT re-emit:
- aliases already listed in the individuals' `aliases` field
- definitions already captured for classes (those have `definition`
  on the class entry)
- one-sentence summaries already captured for individuals

Re-emit ONLY when there is additional structured content: an identifier
with a naming system, a definition with a stated source, an external
cross-reference, etc.

For each representation:
- id:                  short slug (lowercase, hyphenated, unique within
                       this doc)
- represents:          id from one of the lists below
- represents_kind:     "individual" | "activity" |
                       "class_of_individual" | "class_of_activity" |
                       "role"
- representation_kind: "identifier" | "name" | "alias" | "description" |
                       "definition" | "cross_reference"
- value:               the textual content
                       (e.g. "P-101", "ISO 9001:2015",
                       "ACME Corporation Ltd.",
                       "EN 13480-3 Section 6.2")
- system:              optional — the naming or identification system
                       (e.g. "ISO", "ANSI", "internal_tag", "url",
                       "doi", "iec"), or "" if not stated
- description:         one short phrase of context, or ""
- evidence:            verbatim quote from the document

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted individuals:
{individual_id_label_kind_table}
- already-extracted activities:
{activity_id_label_summary_table}
- already-extracted classes of individual:
{class_of_individual_id_label_table}
- already-extracted classes of activity:
{class_of_activity_id_label_table}
- already-extracted roles:
{role_id_label_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "representations": [
    {
      "id":                  "...",
      "represents":          "...",
      "represents_kind":     "individual" | "activity" |
                             "class_of_individual" |
                             "class_of_activity" | "role",
      "representation_kind": "identifier" | "name" | "alias" |
                             "description" | "definition" |
                             "cross_reference",
      "value":               "...",
      "system":              "",
      "description":         "",
      "evidence":            "..."
    }
  ]
}

If no representations are extractable beyond what earlier prompts already
captured, return {"representations": []}.
```

## Converter mapping

| `representation_kind` | Part 2 reification |
|---|---|
| identifier | `Identification` |
| name | `Identification` + `dg:nameKind "name"` |
| alias | `Identification` + `dg:nameKind "alias"` (also `skos:altLabel` shortcut) |
| description | `Description` |
| definition | `Definition` |
| cross_reference | `RepresentationOfThing` + `dg:externalRef` literal |

```turtle
ext:rep-001  a iso15926:Identification ;
    iso15926:hasRepresented ext:p-101 ;
    iso15926:representationValue "P-101" ;
    dg:system "internal_tag" ;
    dg:evidence "Pump P-101 (tag P-101) was inspected." .

ext:rep-002  a iso15926:Identification ;
    iso15926:hasRepresented ext:iso-9001-standard ;
    iso15926:representationValue "ISO 9001:2015" ;
    dg:system "ISO" ;
    dg:evidence "ISO 9001:2015 specifies requirements…" .

ext:rep-003  a iso15926:RepresentationOfThing ;
    iso15926:hasRepresented ext:pressure-vessel-design ;
    dg:externalRef "EN 13480-3 Section 6.2" ;
    dg:evidence "Designed to EN 13480-3 Section 6.2." .
```

Exact property names verified against the OWL at converter implementation time.

The source document itself gets a default `Identification` entry minted
by the converter from the registered file metadata (slug + sha hash) —
independent of this prompt's output.

## Decisions

- Skip condition broad: only skip if no identifiers AND no class-defs AND
  no individuals. In practice runs on every non-trivial document.
  2026-04-29.
- Explicit "do NOT re-emit" rules kept; they prevent duplication with
  #3/#4/#5 while still allowing additional structured content. 2026-04-29.
- `cross_reference` kept as its own representation_kind. Common in
  standards documents. 2026-04-29.
