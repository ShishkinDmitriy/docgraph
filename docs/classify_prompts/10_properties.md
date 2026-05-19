# Prompt #10 — Properties

**Purpose**: extract qualitative / non-numeric properties of things —
color, condition, status, material kind, etc. Numeric properties with
units go to prompt #11.

**Skip condition**: prompt #1 says `has_properties` is `false`.

**Part 2 §**: 5.2.26 Properties; 5.2.27 Classes of property.

**Note on Part 2's property model**: Part 2 reifies properties — a property
is itself an entity (`property` class), classified by a `ClassOfProperty`
(the kind: Color, Status), and inhering in a bearer (the individual or
class it belongs to). This prompt covers categorical/textual values;
quantitative ones (number + unit) go to #11.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + individuals
(incl. `new_individuals` from #8) + activities (incl. `new_activities`
from #8) + classes-of-individual from #5 + classes-of-activity from #4.

**Outputs**: list of property entries with bearer + kind + value.

## Prompt body

```
You are extracting qualitative properties of things from a document,
mapping to ISO 15926-2.

A qualitative property is a non-numeric attribute, quality, or status:
- "Pump P-101 is red"            (color: red)
- "Vessel V-201 is corroded"     (condition: corroded)
- "the document is approved"     (approval_status: approved)
- "the casing material is steel" (material: steel)
- "operational mode: manual"     (mode: manual)

A property may inhere in:
- a specific individual or activity (most common); OR
- a class, when the document attributes a quality to all members of a
  category ("centrifugal pumps are typically high-speed" → property
  high-speed inheres in class `centrifugal-pump`).

NUMERIC properties (with units like kg, bar, m, V) go to a later prompt;
do NOT extract them here.

For each qualitative property:
- id:            short slug (lowercase, hyphenated, unique within this doc)
- bearer:        id from one of the lists below (individual, activity,
                 class-of-individual, or class-of-activity).
- bearer_kind:   "individual" | "activity" | "class_of_individual" |
                 "class_of_activity"
- property_kind: short noun for the kind of property (e.g. "color",
                 "condition", "material", "operational_mode",
                 "approval_status"). Lowercase, snake_case. Re-use the
                 same string when the same kind appears on multiple
                 bearers.
- value:         the categorical / textual value as it appears
                 (e.g. "red", "corroded", "approved")
- description:   one short phrase, or ""
- evidence:      verbatim quote from the document

Bearer must reference an entity from the lists below. If a property is
attached to something not in those lists, omit it.

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
  "properties": [
    {
      "id":            "...",
      "bearer":        "...",
      "bearer_kind":   "individual" | "activity" |
                       "class_of_individual" | "class_of_activity",
      "property_kind": "...",
      "value":         "...",
      "description":   "...",
      "evidence":      "..."
    }
  ]
}

If no qualitative properties are described, return {"properties": []}.
```

## Converter mapping

```turtle
# Property kind (minted once per distinct property_kind)
ext:color-property  a iso15926:ClassOfProperty ;
    rdfs:label "Color" .

# Individual-borne property
ext:prop-001  a iso15926:Property ;
    rdf:type    ext:color-property ;
    iso15926:propertyOf  ext:p-101 ;
    rdfs:label  "red" ;
    dg:value    "red" ;
    dg:evidence "Pump P-101 is painted red." .

# Class-borne property — attaches to the class itself
ext:color-property  rdfs:domain ext:centrifugal-pump .   # if relevant
# OR a reified class-property assertion via dg:propertyOfClass
```

When the bearer is a class, the converter emits a class-level property
attachment (exact form decided at converter implementation time —
options are a domain restriction, an OWL-2 punned individual, or a
`dg:propertyOfClass` shortcut).

The actual Part 2 bearer property name (`propertyOf`, `inheresIn`, …) is
verified against the OWL when implementing the converter.

## Decisions

- Bearer can be an individual, activity, class-of-individual, or
  class-of-activity. Class-level properties allowed where the document
  supports them. 2026-04-29.
- Same `property_kind` string across multiple properties → one shared
  `ClassOfProperty` URI. (Typos will split kinds; accepted tradeoff.)
  2026-04-29.
- Approval-style properties captured here AND in prompt #14. The
  converter deduplicates by URI; both evidence quotes attach to the same
  triple. 2026-04-29.
