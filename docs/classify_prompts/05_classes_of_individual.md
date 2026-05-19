# Prompt #5 — Classes of individual

**Purpose**: extract type-level definitions for individual-side entities —
kinds of person, organization, physical object, document, material, etc.
Mirrors prompt #4 but for the individual side of Part 2.

**Skip condition**: prompt #1 says BOTH `defines_classes` is `false` AND
`describes_individuals` is `false`.

**Part 2 §**: 5.2.7 Classes of individual; 5.2.8 Classes of arranged individual.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + the
individual-id table from prompt #3.

**Outputs**: list of class definitions with kind / parent / instances / evidence.

## Prompt body

```
You are extracting class-level (type) definitions of individuals from a
document, mapping to ISO 15926-2.

A class of individual is a category that some specific thing can be a
member of — not a specific thing itself. You extracted specific things in
an earlier prompt; this prompt extracts the CATEGORIES.

Examples that DEFINE a class:
- "A centrifugal pump is a rotodynamic pump that uses an impeller to..."
- "An invoice is a commercial document issued by a seller to a buyer..."
- "A senior engineer is an engineer with at least 7 years of experience
   and design authority..."
- "Composite materials consist of two or more constituent materials..."

Examples that do NOT define a class:
- "Pump P-101 broke down."           (a specific pump, not the class)
- "John was promoted last year."     (a specific person, not the class)

Categorize each class into ONE of these kinds (the categories mirror those
used for individuals; pick the broadest that fits):

- "person"             — a kind of human (engineer, manager, contractor)
- "organization"       — a kind of organization (vendor, regulator)
- "physical_object"    — a kind of tangible thing (pump, vessel, cable)
- "functional_object"  — a kind of equipment defined by what it does
                         (level controller, safety valve)
- "information_object" — a kind of document or record
                         (invoice, P&ID, audit report)
- "material"           — a kind of substance (composite, particulate,
                         compound, biological matter — fold all into
                         this kind)
- "feature"            — a kind of geometric or structural feature
                         (flange, bevel, hole)
- "organism"           — a kind of living thing other than human
- "arranged_individual"— a kind of arrangement / configuration of things
- "other"              — none of the above; explain in `note`

For each class:
- id:          short slug (lowercase, hyphenated, unique within this doc)
- label:       short human-readable name
- kind:        one of the categories above
- definition:  the document's own definition, paraphrased to one or two
               sentences (no quotes; clean prose)
- parent:      id of a parent class defined elsewhere in this prompt's
               output, or null. Use this for taxonomy ("a centrifugal
               pump is a kind of pump").
- instances:   ids of already-extracted individuals (from prompt #3) that
               are instances of this class. May be []. Use ids exactly
               as given.
- evidence:    verbatim quote from the document
- note:        free-text only when needed (e.g. for "other")

Document context:
- doc_kind: {doc_kind}
- primary_subjects: {primary_subjects}
- already-extracted individuals:
{individual_id_label_kind_table}

Document content:
---
{markdown}
---

Reply with a single JSON object, no prose, no fences:

{
  "classes_of_individual": [
    {
      "id":          "...",
      "label":       "...",
      "kind":        "person" | "organization" | "physical_object" |
                     "functional_object" | "information_object" |
                     "material" | "feature" | "organism" |
                     "arranged_individual" | "other",
      "definition":  "...",
      "parent":      "..." | null,
      "instances":   ["...", "..."],
      "evidence":    "...",
      "note":        ""
    }
  ]
}

If no class-level definitions are found, return
{"classes_of_individual": []}.
```

## Converter mapping

| `kind` | Part 2 metaclass (ClassOf*) |
|---|---|
| person | `ClassOfPerson` |
| organization | `ClassOfOrganization` |
| physical_object | `ClassOfInanimatePhysicalObject` |
| functional_object | `ClassOfFunctionalObject` |
| information_object | `ClassOfInformationObject` |
| material | `ClassOfCompositeMaterial` |
| feature | `ClassOfFeature` |
| organism | `ClassOfOrganism` |
| arranged_individual | `ClassOfArrangedIndividual` |
| other | `ClassOfClassOfIndividual` + `dg:status dg:Unresolved` |

```turtle
ext:centrifugal-pump  a iso15926:ClassOfInanimatePhysicalObject ;
    rdfs:label   "Centrifugal Pump" ;
    rdfs:comment "A rotodynamic pump that uses an impeller…" ;
    rdfs:subClassOf ext:pump ;
    dg:evidence  "A centrifugal pump is…" .

ext:c-005  a iso15926:Classification ;
    iso15926:hasClassifier ext:centrifugal-pump ;
    iso15926:hasClassified ext:p-101 .
```

The `doc_kind` from prompt #1 is **not** duplicated here — the converter
mints `ext:<doc_kind>` as `ClassOfInformationObject` independently when
typing the source document.

## Decisions

- Material subdivisions (`Composite`, `Particulate`, `BiologicalMatter`,
  `Compound`) folded into one `material` kind. 2026-04-29.
- Chemistry-specific classes (`ClassOfAtom`, `ClassOfMolecule`,
  `ClassOfSubAtomicParticle`) dropped entirely. Out of scope for industrial
  / business documents. 2026-04-29.
- Ad-hoc `doc_kind` class lives in the same named graph as the extracted
  entities (cascades on document removal). 2026-04-29.
