# Prompt #7 — Participations

**Purpose**: link specific individuals to specific activities, optionally
with a role concept. This is where the instance-level "who did what in
which activity" graph is constructed.

**Skip condition**: prompt #2 produced zero activities OR (prompt #3
produced zero individuals AND prompt #6 produced zero roles).

**Part 2 §**: 5.2.9 Activities and events — specifically the
`participation` and `class_of_participation` entities.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + activities
table from #2 + individuals table from #3 + roles table from #6.

**Outputs**: list of participation entries linking activity + participant
+ (optional) role.

## Prompt body

```
You are linking individuals to the activities they participate in,
mapping to ISO 15926-2 Participation.

A Participation is the involvement of one individual in one activity,
optionally in a specific role. Each link has exactly one activity, one
participant, and zero-or-one roles.

Examples:
- John (individual) participates in audit-2024 (activity), in the role
  of "lead auditor" (role).
- ACME Corporation (individual) participates in contract-447 (activity),
  in the role of "supplier" (role).
- Pump P-101 (individual) participates in lubrication-jan (activity),
  in the role of "subject equipment" (role) — yes, physical objects
  also have participations.

Only emit participations supported by the document. Do not invent links.
If the same individual participates in the same activity in two different
roles, emit two participations.

For each participation:
- id:          short slug (lowercase, hyphenated, unique within this doc)
- activity:    id of an activity from the list below
- participant: id of an individual from the list below
- role:        id of a role from the list below, or null if no role is
               named for this participation
- description: short phrase describing the participation, or ""
- evidence:    verbatim quote from the document

Use the ids from the lists exactly as given. Do not invent activity,
individual, or role ids.

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted activities:
{activity_id_label_summary_table}
- already-extracted individuals:
{individual_id_label_kind_table}
- already-extracted roles:
{role_id_label_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "participations": [
    {
      "id":          "...",
      "activity":    "...",
      "participant": "...",
      "role":        "..." | null,
      "description": "...",
      "evidence":    "..."
    }
  ]
}

If no participations can be supported, return {"participations": []}.
```

## Converter mapping

```turtle
ext:p-001  a iso15926:Participation ;
    iso15926:hasParticipant ext:john-smith ;
    iso15926:participantInActivity ext:audit-2024-q1 ;
    iso15926:hasRole ext:lead-auditor-role ;
    dg:summary "John served as lead auditor of the Q1 2024 audit." ;
    dg:evidence "John Smith led the Q1 audit." .
```

When a role is set, the converter additionally emits an
`IntendedRoleAndDomain` reifying that the participant fills the role
concept in this specific domain. Skipped when `role: null`. Exact property
names are verified against the OWL at implementation time.

## Decisions

- One participation per (activity, participant, role) triple. Same pair
  with different roles → two participations. 2026-04-29.
- `role` is optional. No-role participation is kept (still a real link).
  2026-04-29.
- `IntendedRoleAndDomain` reification emitted only when a role is present.
  2026-04-29.
