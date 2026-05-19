# Prompt #6 — Roles

**Purpose**: extract role *concepts* — labels like "buyer", "operator",
"supervisor", "auditor". The instance-level "John plays the buyer role in
this transaction" goes to prompt #7 (Participations).

**Skip condition**: prompt #1 says `describes_roles` is `false`.

**Part 2 §**: 5.2.13 Roles and domains; 5.2.24 Possible and intended roles.

**Note on Part 2's role model**: Part 2 has no standalone `Role` class —
only `IntendedRoleAndDomain`, `PossibleRoleAndDomain`, and their `ClassOf*`
counterparts. A role is always a relationship between a *kind of player*
(class of individual) and a *kind of activity* (class of activity). This
prompt emits `ClassOfPossibleRoleAndDomain` instances; prompt #7 emits
`IntendedRoleAndDomain` instances tying specific individuals + activities
+ role concepts.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` +
classes-of-activity table from prompt #4 + classes-of-individual table
from prompt #5.

**Outputs**: list of role concepts with optional `domain` (class of
activity) and `player` (class of individual) links.

## Prompt body

```
You are extracting role concepts from a document, mapping to ISO 15926-2.

A role is a labeled position or function that some entity can fill in
some activity. Examples: "buyer", "seller", "auditor", "pump operator",
"safety officer", "lead engineer".

Extract the role CONCEPTS (the labels). Do NOT extract who fills which
role in which specific activity — that is captured in a later prompt.

For each role:
- id:          short slug (lowercase, hyphenated, unique within this doc)
- label:       short human-readable name (e.g. "Buyer", "Pump Operator")
- description: one-sentence description, or "" if none warranted
- domain:      id of a class-of-activity from the list below if the
               document explicitly says the role applies to that kind of
               activity, else null
- player:      id of a class-of-individual from the list below if the
               document explicitly says the role is filled by that kind
               of entity, else null
- evidence:    verbatim quote from the document

Do not invent classes for `domain` or `player`. If the document is
specific enough to name them but they are not in the lists below, leave
the field null.

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted classes of activity:
{class_of_activity_id_label_table}
- already-extracted classes of individual:
{class_of_individual_id_label_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "roles": [
    {
      "id":          "...",
      "label":       "...",
      "description": "...",
      "domain":      "..." | null,
      "player":      "..." | null,
      "evidence":    "..."
    }
  ]
}

If no roles are described, return {"roles": []}.
```

## Converter mapping

```turtle
ext:buyer-role  a iso15926:ClassOfPossibleRoleAndDomain ;
    rdfs:label   "Buyer" ;
    rdfs:comment "A party purchasing goods or services in a sale." ;
    iso15926:hasDomain ext:sale ;          # if `domain` set
    iso15926:hasPlayer ext:purchaser ;     # if `player` set
    dg:evidence  "The buyer is responsible for…" .
```

Exact property URIs (`hasDomain`, `hasPlayer`) confirmed against the OWL at
converter-implementation time and recorded in the design-doc decision log.

## Decisions

- Two-prompt split: this prompt extracts role concepts only; instance-level
  links go to prompt #7. 2026-04-29.
- `domain` and `player` constrained to already-extracted classes. Roles
  with un-classed domains/players get the field set to null rather than
  invent parallel taxonomy. 2026-04-29.
- Exact OWL property names for `hasDomain` / `hasPlayer` to be verified
  against `docs/ISO-15926-2_2003.rdf` when the converter is implemented.
  2026-04-29.
