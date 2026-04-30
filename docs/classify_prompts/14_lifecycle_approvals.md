# Prompt #14 — Lifecycle & approvals

**Purpose**: extract approval events, lifecycle stages, status changes,
and revisions affecting the entities in the document. Common in
engineering deliverables, regulated industries, document control
systems.

**Skip condition**: prompt #1 says `has_lifecycle_or_approval` is `false`.

**Part 2 §**: 5.2.23 Lifecycle stages and approvals — `approval`,
`class_of_approval`, `class_of_approval_by_status`, `lifecycle_stage`,
`class_of_lifecycle_stage`.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + every
previously extracted entity that could be a subject (individuals,
activities, classes-of-individual, classes-of-activity).

**Outputs**: three parallel lists — approvals, lifecycle stages,
revisions.

## Prompt body

```
You are extracting approval, lifecycle, and revision information from a
document, mapping to ISO 15926-2.

Three kinds of entry:

1. APPROVAL — a recorded act of approving (or rejecting) something.
   "Approved by John Smith on 2024-03-15" / "Rejected pending revision"
   / "Awaiting QA sign-off"

2. LIFECYCLE STAGE — what stage of its life cycle the subject is in.
   "In design phase" / "Commissioned" / "Operational" / "Decommissioned"

3. REVISION — a versioning event.
   "Revision B issued 2024-02-01" / "v2.0 supersedes v1.4"

Many docs have several at once. Extract whichever apply.

For each APPROVAL:
- id:           short slug, unique within this doc
- subject:      id of the entity being approved
- subject_kind: "individual" | "activity" |
                "class_of_individual" | "class_of_activity"
- status:       "approved" | "rejected" | "pending" | "withdrawn" |
                "conditional" | "other"
- by:           id of the person or organization granting/denying
                approval (must be an already-extracted individual), or
                null if unattributed
- when:         ISO-8601 date or natural-language phrase, or null
- description:  one short phrase, or ""
- evidence:     verbatim quote

For each LIFECYCLE STAGE:
- id:           short slug, unique within this doc
- subject:      id of the entity in this stage
- subject_kind: "individual" | "activity" |
                "class_of_individual" | "class_of_activity"
- stage:        short snake_case label for the stage (e.g. "design",
                "construction", "commissioning", "operation",
                "maintenance", "decommissioning", "retired",
                "draft", "issued", "obsolete"). Pick the closest
                term used in the document; lowercase, snake_case.
- when:         ISO-8601 date or natural-language phrase, or null
- description:  one short phrase, or ""
- evidence:     verbatim quote

For each REVISION:
- id:           short slug, unique within this doc
- subject:      id of the revised entity (often the document itself —
                use the source-document id if you see one, or omit
                this entry if you don't)
- subject_kind: "individual" | "activity" |
                "class_of_individual" | "class_of_activity"
- version:      the version label as written ("Rev B", "v2.0",
                "Revision 3", "Issue 04")
- supersedes:   prior version label, or null
- when:         ISO-8601 date or natural-language phrase, or null
- description:  one short phrase, or ""
- evidence:     verbatim quote

Reference subjects and approvers by ids from the lists below. If a
subject or approver is mentioned but not already extracted, omit the
entry rather than invent a new entity.

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

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "approvals": [
    {
      "id":           "...",
      "subject":      "...",
      "subject_kind": "individual" | "activity" |
                      "class_of_individual" | "class_of_activity",
      "status":       "approved" | "rejected" | "pending" |
                      "withdrawn" | "conditional" | "other",
      "by":           "..." | null,
      "when":         "..." | null,
      "description":  "",
      "evidence":     "..."
    }
  ],
  "lifecycle_stages": [
    {
      "id":           "...",
      "subject":      "...",
      "subject_kind": "individual" | "activity" |
                      "class_of_individual" | "class_of_activity",
      "stage":        "...",
      "when":         "..." | null,
      "description":  "",
      "evidence":     "..."
    }
  ],
  "revisions": [
    {
      "id":           "...",
      "subject":      "...",
      "subject_kind": "individual" | "activity" |
                      "class_of_individual" | "class_of_activity",
      "version":      "...",
      "supersedes":   "..." | null,
      "when":         "..." | null,
      "description":  "",
      "evidence":     "..."
    }
  ]
}

If a category yields nothing, return that list empty.
```

## Converter mapping

```turtle
# Approval
ext:appr-001  a iso15926:Approval ;
    iso15926:hasSubject ext:design-spec-rev-b ;
    iso15926:hasApprover ext:john-smith ;
    iso15926:approvalStatus ext:approved-status ;
    iso15926:atTime "2024-03-15"^^xsd:date ;
    dg:evidence "Approved by John Smith on 15 March 2024." .

ext:approved-status  a iso15926:ClassOfApprovalByStatus ;
    rdfs:label "Approved" .

# Lifecycle stage
ext:ls-001  a iso15926:LifecycleStage ;
    iso15926:hasSubject ext:reactor-r1 ;
    rdf:type   ext:operation-stage ;
    iso15926:atTime "2024-01-01"^^xsd:date ;
    dg:evidence "R-1 entered operation on 1 January 2024." .

ext:operation-stage  a iso15926:ClassOfLifecycleStage ;
    rdfs:label "Operation" .

# Revision — no Part 2 Revision class; reuses Identification + dg:supersedes
ext:rev-001  a iso15926:Identification ;
    iso15926:hasRepresented ext:design-spec ;
    iso15926:representationValue "Rev B" ;
    dg:system "revision_label" ;
    dg:supersedes "Rev A" ;
    dg:atTime "2024-02-01"^^xsd:date ;
    dg:evidence "Revision B issued 2024-02-01." .
```

`status` and `stage` strings are minted into `ClassOfApprovalByStatus` and
`ClassOfLifecycleStage` URIs the first time they appear; later entries
reuse them.

Where prompt #10 also captured an approval as a qualitative property, the
converter deduplicates by subject + status into one approval node,
attaching both evidence quotes via multiple `dg:evidence` triples.

## Decisions

- Three parallel lists rather than one discriminated list. Different
  shapes warrant separate keys. 2026-04-29.
- Revision modelled as `Identification` + `dg:supersedes` shortcut.
  Part 2 has no dedicated revision class. 2026-04-29.
- `by` field scoped to approvals only. Lifecycle stages and revisions
  rarely name an actor in source text. 2026-04-29.
