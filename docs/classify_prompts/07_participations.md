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
- intent:      "actual", "intended", or "possible" — see below; default
               "actual"
- activity:    id of an activity from the list below. REQUIRED when
               intent="actual"; OPTIONAL and IGNORED for intent="intended"
               or "possible" (those don't pin down a specific activity).
- participant: id of an individual from the list below
- role:        id of a role from the list below, or null. REQUIRED when
               intent="intended" or "possible"; optional for "actual".
- description: short phrase describing the participation, or ""
- evidence:    verbatim quote from the document

`intent` distinguishes:

- `"actual"` — the document describes a participation that happened or
  is happening: audit logs, "John reviewed the report", "Pump P-101
  pumped fluid in batch 47". The default.
- `"intended"` — the document designates a planned/expected participant
  for a role: contracts, design specs, role assignments.
  Examples: "the auditor for Q3 will be Jane", "ACME is the contracted
  supplier", "Pump P-101 is intended as the primary feed pump".
- `"possible"` — the document describes eligibility / candidacy without
  commitment.
  Examples: "qualified bidders include ACME, BetaCo", "P-101 could
  serve as a backup feed pump".

Default `"actual"`. Choose `"intended"` or `"possible"` only when the
document is clearly forward-looking or hypothetical about *this specific*
person/role pairing.

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
      "intent":      "actual" | "intended" | "possible",
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

| `intent` | Part 2 class | Properties |
|---|---|---|
| `actual` | `iso15926:Participation` (§5.2.9.7) | `hasWhole`=activity, `hasPart`=participant; role attached via `dg:hasRole` shortcut |
| `intended` | `iso15926:IntendedRoleAndDomain` (§5.2.24.3) | `hasPlayer`=participant, `hasPlayed`=role |
| `possible` | `iso15926:PossibleRoleAndDomain` (§5.2.24.4) | `hasPlayer`=participant, `hasPlayed`=role |

```turtle
# intent="actual"
ext:part-001  a iso15926:Participation ;
    iso15926:hasWhole ext:audit-2024-q1 ;
    iso15926:hasPart  ext:john-smith ;
    dg:hasRole ext:role-lead-auditor ;
    dg:evidence "John Smith led the Q1 audit." .

# intent="intended"
ext:irad-002  a iso15926:IntendedRoleAndDomain ;
    iso15926:hasPlayer ext:jane-doe ;
    iso15926:hasPlayed ext:role-auditor ;
    dg:evidence "The auditor for Q3 will be Jane Doe." .

# intent="possible"
ext:prad-003  a iso15926:PossibleRoleAndDomain ;
    iso15926:hasPlayer ext:pump-p101 ;
    iso15926:hasPlayed ext:role-feed-pump ;
    dg:evidence "P-101 could serve as a backup feed pump." .
```

For `intent="intended"` and `intent="possible"`, the entry is **skipped**
when `role` is null (these relationships have no meaning without a played
role concept).

## Decisions

- One participation per (activity, participant, role) triple. Same pair
  with different roles → two participations. 2026-04-29.
- `role` is optional for `intent="actual"`, REQUIRED for `intended` /
  `possible`. No-role actual participation is kept (still a real link).
  2026-04-29.
- For `intent="actual"`, role attached via `dg:hasRole` shortcut rather
  than reifying an additional IntendedRoleAndDomain — the Participation's
  endpoints already carry the same information. 2026-04-29.
- `intent` axis added 2026-05-04 to distinguish actual / intended /
  possible role-and-domain relationships per Part 2 §5.2.24.
