# ISO 15926-2:2003 — Part 2 concept coverage tracker

> **Purpose**: track which of Part 2's 201 entity types ever get instantiated by docgraph's classify pipeline, from what prompt, and verified by which code path.
>
> **Source of truth — concepts**: `/home/dimonina/iso15926-part2.txt` §5.2.
>
> **Source of truth — prompts**: `docs/classify_prompts/01_nature_scan.md` … `14_lifecycle_approvals.md` (each declares its `**Part 2 §**:` line).
>
> **Source of truth — code emission**: the exhaustive list of `iso15926:*` classes that appear as `RDF.type` targets in `src/classify_part2/convert/*.py` and `src/classify_part2/reify.py`.

## Legend

| Column | Meaning |
|---|---|
| **Prompt** | which classify prompt's output triggers emission. `meta` = anchor only. `—` = out of scope. `TBD` = no prompt extracts the input shape that would trigger this concept. |
| **Code** | verified state in the converter code: |
|   `✅` | code emits this exact `iso15926:X` `rdf:type` triple |
|   `🔁` | code emits a docgraph shortcut (`dg:locatedAt`, `dg:hasRole`, `rdfs:subClassOf`, `skos:altLabel`, `rdfs:comment`, …) instead of the Part 2 reified node |
|   `⚠️` | code emits the class but uses it semantically wrong (e.g. modal class used as kind fallback) — see Finding 4 |
|   `📝` | encoded as a typed literal (xsd:date, xsd:dateTime, xsd:integer, xsd:decimal). Part 2 strictly says every number is its own individual; we accept the literal form as a deliberate trade-off. |
|   `❌` | declared by a prompt but no converter emits it — **either an aspirational prompt-doc claim or a real implementation gap** |
|   `meta` | Part 2 root / supertype; never expected as instance |
|   `—` | parked — no decision yet (e.g. EXPRESS primitive types) or intentionally out of scope |

## Decisions captured 2026-05-03

- **Numbers as `xsd:integer` / `xsd:decimal` literals — accepted**. ISO Part 2 strictly says "2 is an individual, 3 is an individual, …" — we knowingly diverge. §5.2.5 rows marked 📝 are *not* gaps.
- **EXPRESS_xxx (5.2.18.1–7) — parked**. No decision yet; left as `—`.
- **Modal × perspective × identity classes must stack — current converter conflates them** (defect, see Finding 4):
  - **Modal axis (§5.2.6.1, §5.2.6.11)**: `ActualIndividual` = "really exists in the world"; `PossibleIndividual` = "planned / designed / not yet existing". Every individual should carry exactly one of these. Today: never emitted.
  - **Perspective axis (§5.2.6.15, §5.2.6.14)**: `WholeLifeIndividual` = "this URI denotes the whole life of the entity"; `TemporalWholePart` = "this URI denotes a specific time-slice". Almost every individual is a `WholeLifeIndividual` view by default. Today: emitted only for `kind=person` / `kind=organization` and used *as the kind*, not as a perspective.
  - **Identity / kind axis (§5.2.6.7, §5.2.6.10, §5.2.6.12, §5.2.6.13, …)**: `PhysicalObject`, `FunctionalPhysicalObject`, `SpatialLocation`, `Stream`, etc. — the "what is it". Today: emitted, but for persons/orgs it's *replaced* by the perspective class.
  - **Specific class axis (5.2.8.*)**: the minted `ClassOf*` (e.g. `:cls/centrifugal-pump`). Today: emitted.
  - Correct stacking for "the centrifugal pump P-101": `iso:FunctionalPhysicalObject` (kind) + `iso:ActualIndividual` (modal) + `iso:WholeLifeIndividual` (perspective) + `:cls/centrifugal-pump` (specific). Currently we emit only the first and last.
| **Trigger / notes** | what kind of source content actually causes this concept to appear in the graph (or, for `❌`/`TBD`, what would have to change). |

## Coverage summary

| Section | Title | Concepts | ✅ | 🔁 | 📝 | ❌ | meta | — |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 5.2.1  | Things | 2 | | | | | 2 | |
| 5.2.2  | Classes | 4 | | 1 | | 1 | 2 | |
| 5.2.3  | Classes of class | 4 | | | | 4 | | |
| 5.2.4  | Multidimensional objects | 2 | | | | 2 | | |
| 5.2.5  | Numbers | 12 | | | 4 | 4 | | 4 |
| 5.2.6  | Possible individuals | 15 | 11 | | | 4 | | |
| 5.2.7  | Classes of individual | 13 | 4 | 2 | | 7 | | |
| 5.2.8  | Classes of arranged individual | 18 | 9 | | | 9 | | |
| 5.2.9  | Activities and events | 10 | 7 | | | 3 | | |
| 5.2.10 | Classes of activity | 6 | 1 | | | 5 | | |
| 5.2.11 | Relationships | 2 | | | | | 2 | |
| 5.2.12 | Classes of relationship | 4 | | | | | 4 | |
| 5.2.13 | Roles and domains | 7 | | 1 | | 6 | | |
| 5.2.14 | Classes of class of relationship | 3 | | | | 3 | | |
| 5.2.15 | Functions | 3 | | | | 3 | | |
| 5.2.16 | Representations of things | 6 | 3 | 1 | | 2 | | |
| 5.2.17 | Classes of representation | 8 | 1 | | | 7 | | |
| 5.2.18 | EXPRESS and UTC representations | 8 | | | 1 | | | 7 |
| 5.2.19 | Classes of class of representation | 11 | | | | 11 | | |
| 5.2.20 | Namespaces | 6 | | | | 6 | | |
| 5.2.21 | Connections | 8 | 3 | | | 5 | | |
| 5.2.22 | Relative locations and sequences | 6 | 1 | 1 | | 4 | | |
| 5.2.23 | Lifecycle stages and approvals | 5 | 4 | | | 1 | | |
| 5.2.24 | Possible and intended roles | 4 | 1 | 1 | | 2 | | |
| 5.2.25 | Set operations | 4 | | | | 4 | | |
| 5.2.26 | Properties | 6 | 1 | | | 5 | | |
| 5.2.27 | Classes of property | 9 | 4 | | | 5 | | |
| 5.2.28 | Scale conversions | 4 | 1 | | | 3 | | |
| 5.2.29 | Shapes | 11 | | | | 11 | | |
| **Total** | | **201** | **49** | **6** | **5** | **129** | **10** | **11** |

> **Headline**: of 201 Part 2 entities, code currently emits 49 as Part 2-typed instances. Six more are covered by docgraph shortcut predicates, five are encoded as typed literals. **129 concepts have no code path** (most of those have a prompt that *declares* it but no converter that *emits* it — the prompt-doc / converter mismatch is the core finding).

---

## 5.2.1 Things

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.1.1 | `abstract_object` | meta | meta | root of the abstract side of the lattice; never instantiated |
| 5.2.1.2 | `thing` | meta | meta | root of everything; never instantiated |

## 5.2.2 Classes

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.2.1 | `class` | meta | meta | parent of every minted class; only `rdfs:subClassOf` chain anchors here |
| 5.2.2.2 | `class_of_abstract_object` | meta | meta | anchor only |
| 5.2.2.3 | `classification` | meta (reify) | ❌ | `reify.classification()` is **defined but never called** — every classification emitted today is a plain `rdf:type` triple. Reified `Classification` would be needed for *third-party* assertions (doc A says "X is classified as Y by org Z"). |
| 5.2.2.4 | `specialization` | meta (reify) | 🔁 | every `rdfs:subClassOf` triple (see `convert/classes.py:123`, `reify.py:67`); never reified as a `Specialization` node — same gap as `Classification` |

## 5.2.3 Classes of class

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.3.1 | `class_of_class` | TBD | ❌ | would trigger when a meta-document defines what *kinds* of class exist (e.g. "Document type is a kind of class") |
| 5.2.3.2 | `class_of_classification` | TBD | ❌ | not extracted; would need explicit prompt asking "does this doc define kinds of `Classification`?" |
| 5.2.3.3 | `class_of_property_space` | TBD | ❌ | when a doc defines a property-kind taxonomy ("flow-rate values are in the volumetric-flow space"); P11 sees the unit but doesn't reify the space |
| 5.2.3.4 | `class_of_specialization` | TBD | ❌ | needed when source defines kinds of `Specialization` |

## 5.2.4 Multidimensional objects

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.4.1 | `class_of_multidimensional_object` | TBD | ❌ | precondition for vector quantities (5.2.5.7, 5.2.26.4); no current path |
| 5.2.4.2 | `multidimensional_object` | TBD | ❌ | as above |

## 5.2.5 Numbers

P11 emits *property* nodes whose value is held as a plain `Literal` (see `convert/properties.py:100`). It does **not** mint Number entities. The `lower_bound_of_property_range` / `upper_bound_of_property_range` it does emit live in §5.2.27, not here.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.5.1 | `arithmetic_number` | — | — | abstract supertype |
| 5.2.5.2 | `boundary_of_number_space` | — | — | abstract |
| 5.2.5.3 | `class_of_number` | — | — | abstract |
| 5.2.5.4 | `enumerated_number_set` | — | ❌ | rare; "discrete value list" |
| 5.2.5.5 | `integer_number` | P11 | 📝 | encoded as `xsd:integer` literal on a `Property` |
| 5.2.5.6 | `lower_bound_of_number_range` | P11 | ❌ | code emits `LowerBoundOfPropertyRange` (5.2.27.4) instead — never the Number-side bound |
| 5.2.5.7 | `multidimensional_number` | — | ❌ | depends on 5.2.4 (TBD) |
| 5.2.5.8 | `multidimensional_number_space` | — | ❌ | depends on 5.2.4 (TBD) |
| 5.2.5.9 | `number_range` | P11 | ❌ | code reifies `PropertyRange` instead — see 5.2.27.6 |
| 5.2.5.10 | `number_space` | P11 | 📝 | implicit (the reals); never reified |
| 5.2.5.11 | `real_number` | P11 | 📝 | encoded as `xsd:decimal` literal |
| 5.2.5.12 | `upper_bound_of_number_range` | P11 | ❌ | as 5.2.5.6 — code emits `UpperBoundOfPropertyRange` |

## 5.2.6 Possible individuals

P03 dispatches by `kind` to emit one of `WholeLifeIndividual` / `PhysicalObject` / `FunctionalPhysicalObject` / `SpatialLocation` / `Stream` / `ActualIndividual` (`convert/individuals.py:45`). P02 emits `PeriodInTime` for non-ISO temporal phrases (`convert/activities.py:107`). P08 emits the composition variants.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.6.1 | `actual_individual` | P03 | ✅ | emitted as the modal axis on every individual whose P03 entry has `existence != "possible"` (default). |
| 5.2.6.2 | `arranged_individual` | P03 | ❌ | the "structured individual" supertype; code uses concrete subclasses |
| 5.2.6.3 | `arrangement_of_individual` | P08 | ❌ | composition reifier; code maps every relation_kind to `CompositionOfIndividual` / `TemporalWholePart` / `FeatureWholePart`, never `ArrangementOfIndividual` |
| 5.2.6.4 | `assembly_of_individual` | P08 | ❌ | as 5.2.6.3 |
| 5.2.6.5 | `composition_of_individual` | P08 | ✅ | every "spatial" / "informational" / "other" whole-part link |
| 5.2.6.6 | `feature_whole_part` | P08 | ✅ | every "feature" relation_kind |
| 5.2.6.7 | `functional_physical_object` | P03 | ✅ | when `kind="functional_object"` — "the centrifugal pump P-101" |
| 5.2.6.8 | `materialized_physical_object` | P03 | ❌ | not in the `_KIND_MAP` — gap; "there is matter making it up" is rarely a primary fact |
| 5.2.6.9 | `period_in_time` | P02 | ✅ | when a begin/end phrase fails ISO-8601 parse (e.g. "Q3 2025") — also marked `dg:status dg:Unresolved` |
| 5.2.6.10 | `physical_object` | P03 | ✅ | when `kind="physical_object"` |
| 5.2.6.11 | `possible_individual` | P03 | ✅ | emitted as the modal axis when P03 sets `existence: "possible"` (planned / designed / proposed / forward-looking). |
| 5.2.6.12 | `spatial_location` | P03 | ✅ | when `kind="location"` — named places |
| 5.2.6.13 | `stream` | P03 | ✅ | when `kind="stream"` — named flows |
| 5.2.6.14 | `temporal_whole_part` | P08 | ✅ | when `relation_kind="temporal"` — today only sub-activity composition. Object-during-period ("Pump P-101 during Q3 2024") is a real Part 2 use of this class but no current path; design notes in `08_whole_parts.md` § "Future patterns — temporal slices". |
| 5.2.6.15 | `whole_life_individual` | P03/P12 | ✅ | emitted as the perspective axis on every P03 individual (set-based dedup means `kind="person"` / `"organization"` get one triple, not two). Also the sign in P12 reified identifiers. |

## 5.2.7 Classes of individual

P04 emits the time-side variants (`ClassOfEvent`, `ClassOfPeriodInTime`, `ClassOfPointInTime`); P05's `kind="other"` falls through to `ClassOfClassOfIndividual` (`convert/classes.py:36`). The composition-class variants are **never** emitted.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.7.1 | `class_of_arrangement_of_individual` | P05/P08 | ❌ | no code path; an explicit "kind of arrangement" definition would land in P05 default |
| 5.2.7.2 | `class_of_assembly_of_individual` | P05/P08 | ❌ | as 5.2.7.1 |
| 5.2.7.3 | `class_of_class_of_composition` | P05/P08 | ❌ | as 5.2.7.1 |
| 5.2.7.4 | `class_of_class_of_individual` | P05 | ✅ | meta-fallback for P05 when `kind="other"` (or location/stream — see `convert/individuals.py:50–52`) |
| 5.2.7.5 | `class_of_composition_of_individual` | P05/P08 | ❌ | as 5.2.7.1 |
| 5.2.7.6 | `class_of_event` | P04 | ✅ | when prompt-4 entry has `iso_class="ClassOfEvent"` |
| 5.2.7.7 | `class_of_feature_whole_part` | P05/P08 | ❌ | no path |
| 5.2.7.8 | `class_of_individual` | P05 | ❌ | the supertype; code uses the more specific 5.2.8.* classes |
| 5.2.7.9 | `class_of_period_in_time` | P05 | ✅ | when prompt-4 entry has `iso_class="ClassOfPeriodInTime"` |
| 5.2.7.10 | `class_of_point_in_time` | P05 | ✅ | when prompt-4 entry has `iso_class="ClassOfPointInTime"` |
| 5.2.7.11 | `class_of_status` | P14 | 🔁 | code mints a `ClassOfApprovalByStatus` subclass (5.2.23.3) labeled with the status string instead — works for approval status, but a non-approval status (e.g. "Operational") has no path |
| 5.2.7.12 | `class_of_temporal_whole_part` | P05/P08 | ❌ | no path |
| 5.2.7.13 | `status` | P14 | 🔁 | status string lifted to the label of a `ClassOfApprovalByStatus` — never instantiated as its own `Status` node |

## 5.2.8 Classes of arranged individual

P05 dispatches by `kind` to mint a `ClassOf*` instance (`convert/classes.py:26–37`). P12 mints `ClassOfInformationRepresentation` per identifier system (e.g. `iban`, `steuernummer`).

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.8.1 | `class_of_arranged_individual` | P05 | ✅ | when P05 `kind="arranged_individual"` |
| 5.2.8.2 | `class_of_atom` | P05 | ❌ | not in `_INDIVIDUAL_KINDS`; would fall through to "other" → 5.2.7.4 |
| 5.2.8.3 | `class_of_biological_matter` | P05 | ❌ | as 5.2.8.2 |
| 5.2.8.4 | `class_of_composite_material` | P05 | ✅ | when `kind="material"` |
| 5.2.8.5 | `class_of_compound` | P05 | ❌ | as 5.2.8.2 |
| 5.2.8.6 | `class_of_feature` | P05 | ✅ | when `kind="feature"` |
| 5.2.8.7 | `class_of_functional_object` | P05 | ✅ | when `kind="functional_object"` (the *type* "centrifugal pump") |
| 5.2.8.8 | `class_of_inanimate_physical_object` | P05 | ✅ | when `kind="physical_object"` |
| 5.2.8.9 | `class_of_information_object` | P05 | ✅ | when `kind="information_object"` — the document-type class for `:Invoice`, etc. (cf. ARCHITECTURE.md) |
| 5.2.8.10 | `class_of_information_presentation` | P05 | ❌ | font/colour/layout dimensions not extracted |
| 5.2.8.11 | `class_of_molecule` | P05 | ❌ | as 5.2.8.2 |
| 5.2.8.12 | `class_of_organism` | P05 | ✅ | when `kind="organism"` |
| 5.2.8.13 | `class_of_organization` | P05 | ✅ | when `kind="organization"` |
| 5.2.8.14 | `class_of_particulate_material` | P05 | ❌ | as 5.2.8.2 |
| 5.2.8.15 | `class_of_person` | P05 | ✅ | when `kind="person"` |
| 5.2.8.16 | `class_of_sub_atomic_particle` | P05 | ❌ | as 5.2.8.2 |
| 5.2.8.17 | `crystalline_structure` | P05 | ❌ | as 5.2.8.2 |
| 5.2.8.18 | `phase` | P05 | ❌ | as 5.2.8.2 |

## 5.2.9 Activities and events

P02 emits the activity side (`Activity`, `Event`, `Beginning`, `Ending`, `PointInTime`); P07 emits `Participation`; P09 emits `CauseOfEvent`. The `Recognition` / `InvolvementByReference` family has no code path.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.9.1 | `activity` | P02 | ✅ | every prompt-2 entry whose `iso_class != "Event"` |
| 5.2.9.2 | `beginning` | P02 | ✅ | per `entry.begin` (`convert/activities.py:70`) |
| 5.2.9.3 | `cause_of_event` | P09 | ✅ | when `relation_kind="causes"` |
| 5.2.9.4 | `ending` | P02 | ✅ | per `entry.end` |
| 5.2.9.5 | `event` | P02 | ✅ | when `iso_class="Event"` |
| 5.2.9.6 | `involvement_by_reference` | P07 | ❌ | code emits `Participation` for everything; no path for "X is mentioned in activity Y but didn't participate" |
| 5.2.9.7 | `participation` | P07 | ✅ | every prompt-7 row |
| 5.2.9.8 | `point_in_time` | P02 | ✅ | when temporal phrase parses as ISO-8601 |
| 5.2.9.9 | `recognition` | P14 | ❌ | not extracted; would need a prompt-14 case for "X recognised Y" distinct from approval |
| 5.2.9.10 | `temporal_bounding` | P02 | ❌ | `Beginning` and `Ending` are subClasses of this in Part 2; the parent class is never emitted directly |

## 5.2.10 Classes of activity

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.10.1 | `class_of_activity` | P04 | ✅ | when prompt-4 entry has `iso_class="ClassOfActivity"` (the default) |
| 5.2.10.2 | `class_of_cause_of_beginning_of_class_of_individual` | P04 | ❌ | rare class-level causality; no path |
| 5.2.10.3 | `class_of_cause_of_ending_of_class_of_individual` | P04 | ❌ | as 5.2.10.2 |
| 5.2.10.4 | `class_of_involvement_by_reference` | P07 | ❌ | as 5.2.9.6 |
| 5.2.10.5 | `class_of_participation` | P07 | ❌ | "the kind of participation called 'buyer-in-purchase'" — currently a `ClassOfPossibleRoleAndDomain` substitutes (P06) |
| 5.2.10.6 | `class_of_recognition` | P14 | ❌ | as 5.2.9.9 |

## 5.2.11 Relationships

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.11.1 | `other_relationship` | meta | meta | escape hatch in the standard; never instantiated by docgraph |
| 5.2.11.2 | `relationship` | meta | meta | parent of every reified relationship |

## 5.2.12 Classes of relationship

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.12.1 | `class_of_assertion` | meta | meta | anchor only |
| 5.2.12.2 | `class_of_relationship` | meta | meta | anchor only |
| 5.2.12.3 | `class_of_relationship_with_related_end_1` | meta | meta | end-1 signature class |
| 5.2.12.4 | `class_of_relationship_with_related_end_2` | meta | meta | end-2 signature class |

## 5.2.13 Roles and domains

P06 emits `ClassOfPossibleRoleAndDomain` (which lives in §5.2.24, not §5.2.13). Nothing in §5.2.13 is currently emitted.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.13.1 | `cardinality` | TBD | ❌ | cardinality declarations on relationship classes — not extracted |
| 5.2.13.2 | `class_of_relationship_with_signature` | TBD | ❌ | the `(relation, end1Class, end2Class)` signature is never reified — Part 2 says role *exists as a relationship* and this is the relationship class for it |
| 5.2.13.3 | `participating_role_and_domain` | P06 | ❌ | not emitted; P06 jumps directly to the `ClassOfPossibleRoleAndDomain` shorthand |
| 5.2.13.4 | `role` | TBD | 🔁 | per-instance role currently shortcutted via `dg:hasRole` from a `Participation` (`convert/participations.py:50`); never reified as a Role node |
| 5.2.13.5 | `role_and_domain` | P06 | ❌ | role tied to a class-of-individual — P06's emitted class is the class-side (5.2.24.2) only |
| 5.2.13.6 | `specialization_by_domain` | P06 | ❌ | role narrowing by domain — no path |
| 5.2.13.7 | `specialization_by_role` | P06 | ❌ | as 5.2.13.6 |

## 5.2.14 Classes of class of relationship

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.14.1 | `class_of_class_of_relationship` | TBD | ❌ | no path; needed when a doc defines a relationship-kind taxonomy |
| 5.2.14.2 | `class_of_class_of_relationship_with_signature` | TBD | ❌ | as 5.2.14.1 with signature |
| 5.2.14.3 | `class_of_scale` | TBD | ❌ | scale-kind taxonomies (kg-scale, °C-scale) — only `Scale` is minted, never `ClassOfScale` |

## 5.2.15 Functions

Mathematical mappings between domains. Engineering "function the part performs" lives in §5.2.8 (`class_of_functional_object`), not here.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.15.1 | `class_of_functional_mapping` | TBD | ❌ | function-kind taxonomy; would be needed to define families of unit conversions |
| 5.2.15.2 | `class_of_isomorphic_functional_mapping` | TBD | ❌ | as 5.2.15.1 |
| 5.2.15.3 | `functional_mapping` | TBD | ❌ | overlaps semantically with §5.2.28 scales / scale-conversions; the existing code uses a single normalised-unit string instead |

## 5.2.16 Representations of things

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.16.1 | `definition` | P04/P05/P12 | ✅ | when P12 sees `representation_kind="definition"` (`convert/identifiers.py:39`); also P04/P05 store class definitions as `rdfs:comment` (no reified Definition) |
| 5.2.16.2 | `description` | P12 | 🔁 | when `representation_kind="description"`, code skips reification and writes plain `rdfs:comment` (`convert/identifiers.py:71`) |
| 5.2.16.3 | `identification` | P12/P14 | ✅ | per identifier (P12), per revision/version label (P14) |
| 5.2.16.4 | `representation_of_thing` | P12 | ✅ | when `representation_kind="cross_reference"` |
| 5.2.16.5 | `responsibility_for_representation` | P12 | ❌ | "ACME assigned tag P-101" — prompt-12 captures the *what* but not the *who-assigned-it* |
| 5.2.16.6 | `usage_of_representation` | P12 | ❌ | "the operations team uses tag P-101" — same gap as 5.2.16.5 |

## 5.2.17 Classes of representation

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.17.1 | `class_of_definition` | P12 | ❌ | definition-pattern classes — no code path |
| 5.2.17.2 | `class_of_description` | P12 | ❌ | as 5.2.17.1 |
| 5.2.17.3 | `class_of_identification` | P12 | ❌ | identifier-system kinds — no code path; the `system` string only mints a `ClassOfInformationRepresentation`, not a `ClassOfIdentification` |
| 5.2.17.4 | `class_of_information_representation` | P12/P14 | ✅ | per distinct identifier system (`iban`, `steuernummer`); also for revision-label form-class (P14) |
| 5.2.17.5 | `class_of_representation_of_thing` | P12 | ❌ | parent class, never emitted |
| 5.2.17.6 | `class_of_representation_translation` | P12 | ❌ | translation classes; rare |
| 5.2.17.7 | `class_of_responsibility_for_representation` | P12 | ❌ | as 5.2.16.5 |
| 5.2.17.8 | `class_of_usage_of_representation` | P12 | ❌ | as 5.2.16.6 |

## 5.2.18 EXPRESS and UTC representations

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.18.1 | `EXPRESS_Boolean` | — | — | EXPRESS-schema artefact |
| 5.2.18.2 | `EXPRESS_binary` | — | — | as 5.2.18.1 |
| 5.2.18.3 | `EXPRESS_integer` | — | — | overlaps 5.2.5.5 (📝 literal) |
| 5.2.18.4 | `EXPRESS_logical` | — | — | EXPRESS-schema artefact |
| 5.2.18.5 | `EXPRESS_real` | — | — | overlaps 5.2.5.11 (📝 literal) |
| 5.2.18.6 | `EXPRESS_string` | — | — | EXPRESS-schema artefact |
| 5.2.18.7 | `class_of_EXPRESS_information_representation` | — | — | EXPRESS-schema artefact |
| 5.2.18.8 | `representation_of_Gregorian_date_and_UTC_time` | P02/P11/P14 | 📝 | every parsed timestamp becomes an `xsd:date` / `xsd:dateTime` literal — **never** a Part 2 reified `RepresentationOfGregorianDateAndUTCTime` node |

## 5.2.19 Classes of class of representation

This is the highest-priority gap for the docgraph file→document chain. ARCHITECTURE.md describes the chain in prose but no code emits any of these nodes.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.19.1 | `class_of_class_of_definition` | TBD | ❌ | meta-meta over definitions; rare |
| 5.2.19.2 | `class_of_class_of_description` | TBD | ❌ | rare |
| 5.2.19.3 | `class_of_class_of_identification` | TBD | ❌ | rare |
| 5.2.19.4 | `class_of_class_of_information_representation` | TBD | ❌ | parent of doc-definition / language / repr-form |
| 5.2.19.5 | `class_of_class_of_representation` | TBD | ❌ | rare meta-meta |
| 5.2.19.6 | `class_of_class_of_representation_translation` | TBD | ❌ | rare |
| 5.2.19.7 | `class_of_class_of_responsibility_for_representation` | TBD | ❌ | rare |
| 5.2.19.8 | `class_of_class_of_usage_of_representation` | TBD | ❌ | rare |
| 5.2.19.9 | `document_definition` | TBD | ❌ | **every ingested file should have one** (template / form spec); the file→document chain is described in ARCHITECTURE.md but not yet reified |
| 5.2.19.10 | `language` | TBD | ❌ | every doc has a language — known at ingest, never typed |
| 5.2.19.11 | `representation_form` | TBD | ❌ | "PDF", "TTL", "Markdown" are known per ingest; never typed |

## 5.2.20 Namespaces

Part 2's `namespace` is reified per-thing. Today docgraph uses RDF/Turtle prefixes in syntax only; nothing about left/right namespace ever lands in the graph.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.20.1 | `class_of_left_namespace` | TBD | ❌ | unused — likely out of scope |
| 5.2.20.2 | `class_of_namespace` | TBD | ❌ | as above |
| 5.2.20.3 | `class_of_right_namespace` | TBD | ❌ | as above |
| 5.2.20.4 | `left_namespace` | TBD | ❌ | as above |
| 5.2.20.5 | `namespace` | TBD | ❌ | as above |
| 5.2.20.6 | `right_namespace` | TBD | ❌ | as above |

## 5.2.21 Connections

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.21.1 | `class_of_connection_of_individual` | P13 | ❌ | "the kind of connection called 'pipe-to-vessel'" — never reified |
| 5.2.21.2 | `class_of_direct_connection` | P13 | ❌ | as 5.2.21.1, direct |
| 5.2.21.3 | `class_of_indirect_connection` | P13 | ❌ | as 5.2.21.1, indirect |
| 5.2.21.4 | `class_of_individual_used_in_connection` | P13 | ❌ | connector-kind classes |
| 5.2.21.5 | `connection_of_individual` | P13 | ❌ | parent class — code always emits a more specific subclass |
| 5.2.21.6 | `direct_connection` | P13 | ✅ | when `connection_kind != "indirect"` (default) |
| 5.2.21.7 | `indirect_connection` | P13 | ✅ | when `connection_kind="indirect"` |
| 5.2.21.8 | `individual_used_in_connection` | P13 | ✅ | when `medium` is set on a P13 entry |

## 5.2.22 Relative locations and sequences

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.22.1 | `class_of_containment_of_individual` | P09 | ❌ | no path |
| 5.2.22.2 | `class_of_relative_location` | P09 | ❌ | no path |
| 5.2.22.3 | `class_of_temporal_sequence` | P09 | ❌ | no path |
| 5.2.22.4 | `containment_of_individual` | P03 | 🔁 | `convert/individuals.py:129` emits `dg:locatedAt`; never reified |
| 5.2.22.5 | `relative_location` | P09 | ❌ | "north of", "above" — not extracted |
| 5.2.22.6 | `temporal_sequence` | P09 | ✅ | every prompt-9 entry except `causes` (which goes to 5.2.9.3) |

## 5.2.23 Lifecycle stages and approvals

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.23.1 | `approval` | P14 | ✅ | every P14 `approvals[*]` entry |
| 5.2.23.2 | `class_of_approval` | P14 | ❌ | parent class; code goes straight to `ClassOfApprovalByStatus` |
| 5.2.23.3 | `class_of_approval_by_status` | P14 | ✅ | one minted per distinct status string ("Approved", "Rejected", …) |
| 5.2.23.4 | `class_of_lifecycle_stage` | P14 | ✅ | one minted per distinct stage label ("Draft", "Final", …) |
| 5.2.23.5 | `lifecycle_stage` | P14 | ✅ | every P14 `lifecycle_stages[*]` entry |

## 5.2.24 Possible and intended roles

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.24.1 | `class_of_intended_role_and_domain` | P06 | ❌ | "the intended buyer is …" the type — no path |
| 5.2.24.2 | `class_of_possible_role_and_domain` | P06 | ✅ | every prompt-6 entry (the only Part 2 type P06 emits) |
| 5.2.24.3 | `intended_role_and_domain` | P07 | ❌ | per-instance intended role — never reified |
| 5.2.24.4 | `possible_role_and_domain` | P07 | 🔁 | `convert/participations.py:50` shortcuts as `dg:hasRole`; no per-participation reified role node |

## 5.2.25 Set operations

A real gap: real-world ontologies frequently use `intersection_of_set_of_class` (e.g. "Manager = Director ∩ Employee"). The pipeline cannot represent this today.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.25.1 | `difference_of_set_of_class` | TBD | ❌ | `A and not B` class definitions |
| 5.2.25.2 | `enumerated_set_of_class` | TBD | ❌ | `{a, b, c}` class definitions |
| 5.2.25.3 | `intersection_of_set_of_class` | TBD | ❌ | very common; e.g. ARCHITECTURE.md's "smith in 24 pt Times New Roman bold" example is exactly this |
| 5.2.25.4 | `union_of_set_of_class` | TBD | ❌ | `A or B` class definitions |

## 5.2.26 Properties

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.26.1 | `class_of_indirect_property` | P10/P11 | ❌ | indirectly-attached property kinds — no path |
| 5.2.26.2 | `comparison_of_property` | P10/P11 | ❌ | "warmer than", "heavier than" — not extracted |
| 5.2.26.3 | `indirect_property` | P10/P11 | ❌ | property of a property — no path |
| 5.2.26.4 | `multidimensional_property` | P11 | ❌ | depends on §5.2.4 (TBD) |
| 5.2.26.5 | `property` | P10/P11 | ✅ | every qualitative or quantitative property entry |
| 5.2.26.6 | `property_quantification` | P11 | ❌ | the binding `Property → Scale` is shortcutted as `dg:onScale`; no `PropertyQuantification` node |

## 5.2.27 Classes of property

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.27.1 | `boundary_of_property_space` | P11 | ❌ | abstract; not minted |
| 5.2.27.2 | `class_of_property` | P10/P11 | ✅ | one minted per distinct `property_kind` / `quantity_kind` string |
| 5.2.27.3 | `enumerated_property_set` | P10 | ❌ | enumerated qualitative values — qualitative property values are stored as `Literal`, not as members of an enumerated set |
| 5.2.27.4 | `lower_bound_of_property_range` | P11 | ✅ | when `min` is set on a P11 entry |
| 5.2.27.5 | `multidimensional_property_space` | P11 | ❌ | depends on §5.2.4 |
| 5.2.27.6 | `property_range` | P11 | ✅ | when `min` and/or `max` are set on a P11 entry |
| 5.2.27.7 | `property_space` | P11 | ❌ | the values-live-in-X space; never reified |
| 5.2.27.8 | `single_property_dimension` | P11 | ❌ | scalar dimension; never reified |
| 5.2.27.9 | `upper_bound_of_property_range` | P11 | ✅ | when `max` is set on a P11 entry |

## 5.2.28 Scale conversions

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.28.1 | `class_of_scale_conversion` | P11 | ❌ | unit-conversion classes — no path |
| 5.2.28.2 | `coordinate_system` | P11 | ❌ | not extracted |
| 5.2.28.3 | `multidimensional_scale` | P11 | ❌ | depends on §5.2.4 |
| 5.2.28.4 | `scale` | P11 | ✅ | one minted per normalised unit string (`kg`, `m³/h`, `°C`, …) |

## 5.2.29 Shapes

Shape model entirely uncovered. No prompt asks for length / width / diameter / shape-class fields.

| § | Concept | Prompt | Code | Trigger / notes |
|---|---|---|---|---|
| 5.2.29.1 | `class_of_dimension_for_shape` | TBD | ❌ | dimension-kind classes |
| 5.2.29.2 | `class_of_shape` | TBD | ❌ | shape kind ("cylinder", "rectangle") |
| 5.2.29.3 | `class_of_shape_dimension` | TBD | ❌ | shape-dimension class |
| 5.2.29.4 | `dimension_of_individual` | TBD | ❌ | per-individual dimension |
| 5.2.29.5 | `dimension_of_shape` | TBD | ❌ | shape-dimension instance |
| 5.2.29.6 | `individual_dimension` | TBD | ❌ | dimension instance |
| 5.2.29.7 | `property_for_shape_dimension` | TBD | ❌ | shape-dim property |
| 5.2.29.8 | `property_space_for_class_of_shape_dimension` | TBD | ❌ | shape-dim property space |
| 5.2.29.9 | `shape` | TBD | ❌ | per-individual shape instance |
| 5.2.29.10 | `shape_dimension` | TBD | ❌ | shape dimension |
| 5.2.29.11 | `specialization_of_individual_dimension_from_property` | TBD | ❌ | dimension specialization |

---

## Findings

The grading above turned up three categories of issue.

### 1. Prompt-doc / converter mismatch — most "covered" rows are aspirational

The prompt files declare "this prompt covers Part 2 §X.Y" but the converter for that prompt only emits a small subset. Examples:
- **§5.2.21 Connections**: P13's prompt body says it covers all eight connection concepts; the converter emits only `DirectConnection` / `IndirectConnection` / `IndividualUsedInConnection`. The class-of-connection variants (5.2.21.1–4) and the parent `connection_of_individual` (5.2.21.5) have no path.
- **§5.2.22 Relative locations**: P09 declares §5.2.22; only `temporal_sequence` is reified; `containment_of_individual` is shortcut to `dg:locatedAt`; the four other concepts have no path.
- **§5.2.5 Numbers**: P11 declares §5.2.5; in practice all numeric values are stored as plain literals — no Number entity is ever minted.
- **§5.2.27 Classes of property**: P11 declares §5.2.27 (9 concepts); only 4 are actually emitted.

Action options for each: (a) extend the converter to emit the missing classes, (b) tighten the prompt-doc claim to only what the converter actually emits, or (c) confirm that the docgraph shortcut is intentionally preferred over Part 2 reification.

### 2. Genuine gaps with high docgraph relevance

These are concepts with no prompt and no converter, but they should arguably exist in any docgraph output:

- **§5.2.19 `document_definition` / `language` / `representation_form`** — every ingested file is an instance of these, but they're never typed. The file→document chain in ARCHITECTURE.md describes the structure in prose; the converter never lays it down.
- **§5.2.25 `intersection_of_set_of_class`** — the canonical "smith in 24 pt Times New Roman bold" example from §4.8.4.1.3 is exactly this; needed as soon as a real ontology with intersection-defined classes is ingested.
- **§5.2.16.5–6 `responsibility_for_representation` / `usage_of_representation`** — for "ACME assigned tag P-101 in 2020" assertions; relevant for any approval / certificate workflow.
- **§5.2.2.3 `Classification`** — `reify.classification()` is defined but never called; the moment a doc asserts a third-party classification ("doc A says X is a Y") this needs to land.

### 3. Reasonable-but-unverified — classes present in `_KIND_MAP` but never tested

Several `_INDIVIDUAL_KINDS` entries (`organism`, `feature`, `material`) are wired but I haven't confirmed any test or fixture document exercises them. Worth a smoke test.

### 4. Modal × perspective × identity classes are conflated (defect)

Per the Decisions section above, every Part 2 individual is meant to carry a stack of orthogonal classifications:

| Axis | Choice | Tells you |
|---|---|---|
| Modal | `ActualIndividual` ↔ `PossibleIndividual` | does it really exist, or is it planned/designed? |
| Perspective | `WholeLifeIndividual` ↔ `TemporalWholePart` (slice) | are we talking about the whole entity, or one time-slice of it? |
| Identity / kind | `PhysicalObject`, `FunctionalPhysicalObject`, `SpatialLocation`, `Stream`, … | what category is it? |
| Specific class | minted `ClassOf*` (e.g. `:cls/centrifugal-pump`) | the concrete class the document defines |

Just one axis is never enough — `ActualIndividual` alone says "real, but real *what*?"; `WholeLifeIndividual` alone says "the whole life of *something*, but of what?"; even both together (`Actual + WholeLife`) still don't say what the thing is.

**Today the converter emits exactly two `rdf:type` triples per individual** (`convert/individuals.py:79–98`):
1. one of {`PhysicalObject`, `FunctionalPhysicalObject`, `SpatialLocation`, `Stream`, `WholeLifeIndividual`, `ActualIndividual`} — chosen by `_KIND_MAP[kind]`;
2. the minted `ClassOf*` specific class.

The first column collapses three different axes into one position:
- For `kind="person"` / `"organization"`: emits `WholeLifeIndividual` (perspective) — so the kind is *only* on the minted `ClassOfPerson` subclass; modal axis is missing entirely.
- For `kind="other"`: emits `ActualIndividual` (modal) as the kind fallback — so a really-existing pump-of-unknown-type and a planned-pump-of-unknown-type would both look identical.
- For everything else: emits a kind class (`PhysicalObject` etc.) — but no modal and no perspective.

**Correct shape** for "the centrifugal pump P-101":
```
:p101  a  iso15926:ActualIndividual ;            # modal: it really exists
       a  iso15926:WholeLifeIndividual ;         # perspective: this URI is its whole-life view
       a  iso15926:FunctionalPhysicalObject ;    # kind
       a  ext:cls/centrifugal-pump .             # specific
```

For a planned pump in a design spec, swap `ActualIndividual` for `PossibleIndividual` and the rest stays.

**Action required**: revise `_KIND_MAP` and the `_emit_individual` body to emit modal + perspective alongside the kind, not in place of it. Add a P03 prompt-output field for the modal axis (default `actual`, set `possible` for design / planning / specification language).

## Next steps

1. **Decide per-row whether each `🔁` shortcut is intentional** — if so, document the trade-off in `ARCHITECTURE.md`; if not, plan the reification.
2. **Pick one of the high-relevance gaps in section 2** and run a candidate document through the pipeline; verify the gap shows up in the output named graph and design the converter extension.
3. **Reconcile prompt claims with converter behavior** — either extend the converters or tighten the prompt docs so the `**Part 2 §**:` line matches what is actually produced.
