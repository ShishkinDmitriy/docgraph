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
                     IMPORTANT: identifiers like IBAN, BIC, email
                     address, phone number do NOT identify the
                     organization or person directly — they identify a
                     *bank account*, a *mailbox*, a *phone line* held
                     by the entity. If an account / mailbox / line
                     individual was extracted, point at it; otherwise
                     point at the closest concrete owner and the model
                     will improve when intermediate entities are
                     extracted.
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
- assigned_by:         optional — id of an already-extracted organization
                       or person who took responsibility for assigning
                       this representation (e.g. "ACME assigned tag
                       P-101", "Crossref assigned this DOI", "ISO
                       maintains this standard"). Leave "" when not stated.
- used_by:             optional — id of an already-extracted organization
                       or person who uses this representation (without
                       necessarily being responsible for it). Distinct
                       from assigned_by — usage does not imply
                       responsibility. Leave "" when not stated.
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
      "assigned_by":         "",
      "used_by":             "",
      "description":         "",
      "evidence":            "..."
    }
  ]
}

If no representations are extractable beyond what earlier prompts already
captured, return {"representations": []}.
```

## Converter mapping

Two emission modes:

**Shortcut path** — `name`, `alias`, `description` are simple labels and
get a direct triple on the target. No reified node, no separate sign.

| `representation_kind` | Triple emitted on target |
|---|---|
| name | `target  skos:prefLabel  "value"` |
| alias | `target  skos:altLabel  "value"` |
| description | `target  rdfs:comment  "value"` |

**Reified path** — `identifier`, `definition`, `cross_reference` produce
a full Part 2 representation with a separate sign individual carrying
the actual text:

```turtle
# 1. The sign — the identifier text as a possible_individual.
ext:sign-iban-de83  a  iso15926:WholeLifeIndividual,
                       ext:cls/iban ;
    rdfs:label "DE83 0060 6010 0065 1388 51" .

# 2. The form-class — one shared per `system` value.
ext:cls/iban  a  iso15926:ClassOfInformationRepresentation ;
    rdfs:label "iban" .

# 3. The Identification relationship.
ext:rep-iban-de83  a  iso15926:Identification ;
    iso15926:hasSign         ext:sign-iban-de83 ;
    iso15926:hasRepresented  <ind/practice-account> ;
    dg:system "iban" ;
    dg:evidence "IBAN: DE83…" .
```

Cross-references skip the sign step and keep an `dg:externalRef` literal
since the target lives outside our graph.

When `assigned_by` is set, a sibling
`iso15926:ResponsibilityForRepresentation` node is minted alongside the
representation:

```turtle
ext:resp-rep-iban-de83  a iso15926:ResponsibilityForRepresentation ;
    iso15926:hasControlled ext:rep-iban-de83 ;
    iso15926:hasController ext:org-acme-bank .
```

`used_by` mints a parallel `iso15926:UsageOfRepresentation` (`hasUsed` /
`hasUser`). Both are independent of the Identification node and either,
both, or neither may be present.

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
