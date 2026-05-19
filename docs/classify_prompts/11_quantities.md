# Prompt #11 — Numbers, scales, units

**Purpose**: extract quantitative properties — numeric values with units.
Distinct from prompt #10 (categorical/textual values).

**Skip condition**: prompt #1 says `has_quantities` is `false`.

**Part 2 §**: 5.2.5 Numbers; 5.2.28 Scale conversions.

**Note on Part 2's quantity model**: a numeric property is a `Property`
instance, classified by a `ClassOfProperty` (the quantity-kind, e.g.
"FlowRate"), inhering in a bearer, with a value drawn from a `Scale`
(the unit, e.g. "m³/h"). Ranges and bounds are reified as
`PropertyRange`, `LowerBoundOfPropertyRange`, `UpperBoundOfPropertyRange`.
The converter handles all reification.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + bearers
(individuals, activities, classes-of-individual, classes-of-activity).

**Outputs**: list of quantity entries with bearer + kind + value
(exact / range / bound) + unit.

## Prompt body

```
You are extracting numeric properties — quantities with units — from a
document, mapping to ISO 15926-2.

A numeric property is a measurable attribute with a unit. Examples:
- "Pump P-101 has a flow rate of 50 m³/h"     → 50 m³/h, exact
- "Maximum pressure: 10 bar"                   → 10 bar, upper bound
- "Operating temperature 60 to 80 °C"          → 60..80 °C, range
- "Tank capacity ≥ 1000 L"                     → 1000 L, lower bound
- "Cable length: 25.4 m"                       → 25.4 m, exact

QUALITATIVE properties (color, status, condition) belong to a different
prompt; do NOT extract them here.

For each quantitative property:
- id:            short slug (lowercase, hyphenated, unique within this doc)
- bearer:        id from one of the lists below
- bearer_kind:   "individual" | "activity" | "class_of_individual" |
                 "class_of_activity"
- quantity_kind: short noun for the kind of quantity (e.g. "flow_rate",
                 "pressure", "temperature", "length", "capacity",
                 "voltage"). Lowercase, snake_case. Re-use the same
                 string when the same kind appears on multiple bearers.
- exact:         numeric value as a string (preserves precision), or null
- min:           lower-bound numeric value as a string, or null
- max:           upper-bound numeric value as a string, or null
- unit:          the unit string as it appears, or "" if dimensionless
                 (e.g. "m³/h", "bar", "°C", "kg", "V", "L", "%", "")
- description:   one short phrase, or ""
- evidence:      verbatim quote from the document

Use exactly one of these patterns:
  - exact set, min and max null         → an exact value
  - min set, exact and max null         → lower bound only (≥)
  - max set, exact and min null         → upper bound only (≤)
  - min and max set, exact null         → a range [min, max]
Bounds are inclusive (no strict inequality distinction).

Preserve the value as a string — do not round, do not normalise units.
"50.0", "1.234e-3", "1,000" stay as written.

Bearer must reference an entity from the lists below. If a quantity is
attached to something not listed, omit it.

## Templates available

If a quantity has an exact value AND the document gives an explicit
effective date for the assertion, prefer emitting a template instance in
the `instances` array (see schema below) instead of a `quantities` row.
Use the existing `quantities` schema for ranges/bounds, and for exact
values where no effective date is given.

Available templates:

{templates}

Use the URI shown in the heading (e.g. `iso:IndividualHasPropertyWithValue`)
as the `template` field. Bind each variable to a CURIE (`ext:p-101`) for
entity references, the `hasPropertyType` and `hasScale` to ad-hoc
`ext:<slug>` URIs you mint for the quantity-kind and unit (re-using the
same slug across instances when the same kind/unit appears), and the
date to an ISO 8601 string. Drop the `quantities` row for any quantity
you emit as a template instance — never duplicate.

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
  "instances": [
    {
      "template": "iso:IndividualHasPropertyWithValue",
      "bindings": {
        "hasPropertyPossessor": "ext:<bearer-id>",
        "hasPropertyType":      "ext:<quantity-kind-slug>",
        "valPropertyValue":     "30.57",
        "hasScale":             "ext:<unit-slug>",
        "valEffectiveDate":     "2021-07-27T00:00:00Z"
      }
    }
  ],
  "quantities": [
    {
      "id":            "...",
      "bearer":        "...",
      "bearer_kind":   "individual" | "activity" |
                       "class_of_individual" | "class_of_activity",
      "quantity_kind": "...",
      "exact":         "..." | null,
      "min":           "..." | null,
      "max":           "..." | null,
      "unit":          "...",
      "description":   "...",
      "evidence":      "..."
    }
  ]
}

If no quantitative properties are described, return
{"instances": [], "quantities": []}.
```

## Converter mapping

```turtle
# ClassOfProperty for the quantity kind
ext:flow-rate-prop  a iso15926:ClassOfProperty ;
    rdfs:label "Flow Rate" .

# Scale for the unit
ext:scale-m3-per-h  a iso15926:Scale ;
    rdfs:label "m³/h" .

# Exact-value quantity
ext:q-001  a iso15926:Property ;
    rdf:type    ext:flow-rate-prop ;
    iso15926:propertyOf  ext:p-101 ;
    iso15926:onScale     ext:scale-m3-per-h ;
    iso15926:numericValue "50"^^xsd:decimal ;
    dg:evidence "Pump P-101 has a flow rate of 50 m³/h." .

# Range-valued quantity
ext:q-002  a iso15926:Property ;
    rdf:type    ext:temperature-prop ;
    iso15926:propertyOf ext:reactor-r1 ;
    iso15926:onScale    ext:scale-celsius ;
    iso15926:hasRange [
        a iso15926:PropertyRange ;
        iso15926:lowerBound [ a iso15926:LowerBoundOfPropertyRange ;
                              iso15926:numericValue "60"^^xsd:decimal ] ;
        iso15926:upperBound [ a iso15926:UpperBoundOfPropertyRange ;
                              iso15926:numericValue "80"^^xsd:decimal ]
    ] ;
    dg:evidence "Operating temperature 60 to 80 °C." .
```

Bound-only cases use only `lowerBound` or `upperBound`. Exact property
names verified against the OWL at converter implementation time.

A small Python lookup table normalises units (`m^3/h` ↔ `m³/h`, `degC` ↔
`°C`) before minting `Scale` URIs, so equivalent units share one `Scale`.

## Decisions

- Flat three-field value shape (`exact` / `min` / `max`). 2026-04-29.
- Strict inequality dropped — Part 2 has no strict-bound distinction in
  standard usage; "< 10" is treated as "≤ 10". 2026-04-29.
- Unit normalisation lives in the converter (Python). LLM emits the unit
  verbatim. 2026-04-29.
