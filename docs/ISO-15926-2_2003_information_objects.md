# ISO 15926-2:2003 — Information Objects (extract)

> Verbatim extract from **ISO 15926-2:2003** (file `2003 - ISO 15926-22003 -- Industrial automation systems .pdf`). Scope: only the entities and narrative needed to model **information objects, representations, and the representation_of_thing relationship family**. For anything outside this scope read the source PDF.
>
> Page numbers below are PDF page numbers from the standard. Section numbers (e.g. §5.2.16.4) are the standard's own numbering.

---

## 1. Conceptual narrative

### §4.8.4.1.3 — Information classes (p. 45–46)

Representation of meaning using symbols depends on using consistent, recognisable patterns. Patterns are classes. A particular writing or rendering, say on a piece of paper or on a video screen that can be observed with our senses, is a **possible_individual** that is a member of a pattern class.

In this part of ISO 15926, a **`class_of_information_representation`** identifies a pattern used to represent information (see §5.2.17.4).

The rendered patterns often have many presentational variations such as colour, font, size, and weight. **`class_of_information_presentation`** describes these variations (see §5.2.8.10).

Members of **`class_of_information_object`** are the combinations of the recognizable patterns and their presentation styles (see §5.2.8.9).

> EXAMPLE — An `arranged_individual` `#smith` that is a member of the `class_of_inanimate_physical_object` "label" *and* a member of the `class_of_information_object` "smith in 24 pt Times New Roman bold". The class "Smith in 24 pt Times New Roman bold" is an intersection of the `class_of_information_representation` "smith" and the presentation classes "Times New Roman", "bold", "24pt".

The literal character patterns defined by ISO 10303 EXPRESS for text strings, reals, integers, binary, logical, Boolean and the ISO 8061 time representation are defined as explicit subtypes of `class_of_information_representation`.

---

### §4.8.4.2.1 — Signs and patterns (p. 47)

Representation is the use of signs and patterns as information. **A sign is a role of a `possible_individual`** — i.e. a space-time extension. Signs can be any individual and can represent any thing.

**`representation_of_thing`** is a *relationship* that indicates that a `possible_individual` is a sign for something else (§5.2.16.4).

Patterns are types or classes of signs, the pattern being the repeatable nature of the member signs. Signs that are members of the same pattern are often used to represent the same thing. So the pattern "Joe Smith" wherever, whenever and how rendered usually refers to the person.

> EXAMPLE — Smith the person as the `actual_individual` `#3578` linked to the sign `#smith` by a `representation_of_thing` relationship. The sign `#smith` is an inanimate `physical_object` that is a member of the "Smith" pattern. The `representation_of_thing` relationship is a member of the `class_of_representation_of_thing` linking the pattern "Smith" to `#3578`.

---

### §4.8.4.2.2 — Identification, description and definition (p. 48)

`identification`, `description` and `definition` are all types of representation that apply to alphanumeric, picture and sound signs (see §5.2.16.1–.3). **Because individuals cannot be defined**, they are what they are, definitions are restricted to **classes**.

In process plant related activities `identification`, `description` and `definition`s are more often declared at the **pattern level**, applying to all signs of the pattern (see §5.2.17.1–.3 — `class_of_identification`, `class_of_description`, `class_of_definition`).

> EXAMPLE — The `arranged_individual` known as "My pump" has been given an identification pattern of "AC-1234". The identification sign "AC-1234" appears on the "name plate" of the pump.

---

### §4.8.4.2.3 — Use and responsibility of representation (p. 49–50)

The use of certain signs and patterns as representations for particular things is discretionary, and may be restricted to certain people and organisations. The same pattern may be used to represent different things by different people or organisations and a particular thing may have several representation patterns assigned and used by different organisations.

This part of ISO 15926 distinguishes **responsibility** from general **use**. Responsibility is used to indicate the person or organisation that took the decision to assign the sign or pattern to the thing that it represents (§5.2.16.5). Use indicates that an organisation or person uses the representation in their activities (§5.2.16.6).

In process plant related activities use and responsibility of representation are more often declared at the pattern level (`class_of_responsibility_for_representation`, `class_of_usage_of_representation` — §5.2.17.7, §5.2.17.8).

---

### §4.8.4.2.4 — Classes of pattern (p. 51)

Patterns may be further abstracted to define rules for the types of thing the member patterns may represent and to define rules constraining the composition of member patterns (see §5.2.19). The classes of pattern are defined as **`class_of_class_of_information_representation`**. Explicit subtypes are given for `language`, `representation_form`, and `document_definition` (§5.2.19.9–.11).

> EXAMPLE 1 — The template used for an engineering data sheet is a `document_definition`.
>
> EXAMPLE 2 — "Hexadecimal" is a `representation_form` that is a specialization of "text" that is also a `representation_form`.
>
> EXAMPLE 3 — A P21 file contains records that represent functional_physical_objects. "P21 file" is a `representation_form` that has "P21 record"s as parts.

---

## 2. Formal entity definitions

All EXPRESS specifications below are verbatim from the standard.

### §5.2.8.9 — `class_of_information_object` (p. 122)

A `class_of_information_object` is a `class_of_arranged_individual` whose members are members of zero or more `class_of_information_representation` and of zero or more `class_of_information_presentation`.

> NOTE — Usually, it is a `physical_object` (like a paper document) that is classified as a `class_of_information_object`.
>
> EXAMPLE — "Newspaper" is a `class_of_information_object`.

```express
ENTITY class_of_information_object
  SUBTYPE OF(class_of_arranged_individual);
END_ENTITY;
```

---

### §5.2.8.10 — `class_of_information_presentation` (p. 123)

A `class_of_information_presentation` is a `class_of_arranged_individual` that distinguishes styles for presenting information.

> EXAMPLE — The character styles bold, italic, Times New Roman, and 16pt can be represented as instances of `class_of_information_presentation`.

```express
ENTITY class_of_information_presentation
  SUBTYPE OF(class_of_arranged_individual);
END_ENTITY;
```

---

### §5.2.16 — Representation relationships (p. 148–150)

#### §5.2.16.1 — `definition`

A `definition` is a `representation_of_thing` that indicates that the **class** is defined by the sign `possible_individual`.

```express
ENTITY definition
  SUBTYPE OF(representation_of_thing);
  SELF\representation_of_thing.represented : class;
END_ENTITY;
```

#### §5.2.16.2 — `description`

A `description` is a `representation_of_thing` that indicates that the `possible_individual` describes the thing.

> EXAMPLE — A copy of the Piping and Instrumentation Diagram for Crude Distillation Unit 1 at refinery X has a `description` relationship with the plant.

```express
ENTITY description
  SUBTYPE OF(representation_of_thing);
END_ENTITY;
```

#### §5.2.16.3 — `identification`

An `identification` is a `representation_of_thing` that indicates that the `possible_individual` is an identifier for the thing identified.

> EXAMPLE 1 — The relationship between the text "P101" on a printed copy of a pump data sheet and the applicable `functional_physical_object`.
>
> EXAMPLE 2 — The relationship between a name tag and an employee wearing it.

```express
ENTITY identification
  SUBTYPE OF(representation_of_thing);
END_ENTITY;
```

#### §5.2.16.4 — `representation_of_thing` ⭐

A `representation_of_thing` is a **relationship** that indicates that a `possible_individual` is a sign for a thing.

> EXAMPLE — The relationship between a nameplate with its serial number and other data, and a particular pressure vessel (`materialized_physical_object`) is an example of `representation_of_thing` that is an `identification`.
>
> NOTE — In general it will be `class_of_representation_of_thing` that will be of interest, rather than each `representation_of_thing`. However, `representation_of_thing` will be of interest when **individual copies of documents are managed and controlled**.

```express
ENTITY representation_of_thing
  SUBTYPE OF(relationship);
  represented : thing;
  sign        : possible_individual;
END_ENTITY;
```

Attributes:
- `represented` — the thing that is represented
- `sign` — the `possible_individual` that is the sign

#### §5.2.16.5 — `responsibility_for_representation`

A relationship indicating that the `controller` `possible_individual` administers the `controlled` `representation_of_thing`.

```express
ENTITY responsibility_for_representation
  SUBTYPE OF(relationship);
  controlled : representation_of_thing;
  controller : possible_individual;
END_ENTITY;
```

#### §5.2.16.6 — `usage_of_representation`

A relationship indicating that the `representation_of_thing` is used by the `possible_individual`. Usage does not imply responsibility.

```express
ENTITY usage_of_representation
  SUBTYPE OF(relationship);
  used : representation_of_thing;
  user : possible_individual;
END_ENTITY;
```

---

### §5.2.17 — Classes of representation (p. 152–155)

#### §5.2.17.1 — `class_of_definition`

A `class_of_representation_of_thing` whose `pattern` is a definition of the `represented` class.

```express
ENTITY class_of_definition
  SUBTYPE OF(class_of_representation_of_thing);
  SELF\class_of_representation_of_thing.represented : class;
END_ENTITY;
```

#### §5.2.17.2 — `class_of_description`

A `class_of_representation_of_thing` whose `pattern` is a description of the `represented` thing.

```express
ENTITY class_of_description
  SUBTYPE OF(class_of_representation_of_thing);
END_ENTITY;
```

#### §5.2.17.3 — `class_of_identification`

A `class_of_representation_of_thing` indicating the pattern is used to refer to the represented thing.

```express
ENTITY class_of_identification
  SUBTYPE OF(class_of_representation_of_thing);
END_ENTITY;
```

#### §5.2.17.4 — `class_of_information_representation` ⭐

A `class_of_arranged_individual` that defines a pattern that represents information.

> EXAMPLE — Texts formed with the pattern of characters 's' concatenated with 'u' concatenated with 'n' are members of the 'sun' `class_of_information_representation`.

```express
ENTITY class_of_information_representation
  SUPERTYPE OF (ONEOF(class_of_EXPRESS_information_representation,
                      representation_of_Gregorian_date_and_UTC_time))
  SUBTYPE OF(class_of_arranged_individual);
END_ENTITY;
```

#### §5.2.17.5 — `class_of_representation_of_thing` ⭐

A `class_of_relationship` that indicates that all members of the pattern `class_of_information_representation` represent the thing.

```express
ENTITY class_of_representation_of_thing
  SUBTYPE OF(class_of_relationship);
  pattern     : class_of_information_representation;
  represented : thing;
END_ENTITY;
```

Attributes:
- `pattern` — the `class_of_information_representation` whose members represent the referenced thing
- `represented` — the thing represented

#### §5.2.17.6 — `class_of_representation_translation`

A `class_of_relationship` indicating the translation of two instances of `class_of_information_representation`.

```express
ENTITY class_of_representation_translation
  SUBTYPE OF(class_of_relationship);
  class_of_first  : class_of_information_representation;
  class_of_second : class_of_information_representation;
END_ENTITY;
```

#### §5.2.17.7 — `class_of_responsibility_for_representation`

A `class_of_relationship` whose members indicate that a `possible_individual` (usually an organization) deems that members of the pattern can be used as representations of the represented thing.

```express
ENTITY class_of_responsibility_for_representation
  SUBTYPE OF(class_of_relationship);
  class_of_controlled : class_of_representation_of_thing;
  controller          : possible_individual;
END_ENTITY;
```

#### §5.2.17.8 — `class_of_usage_of_representation`

A `class_of_relationship` whose members indicate that a `possible_individual` reads or otherwise uses members of the pattern as a representation of the represented thing.

```express
ENTITY class_of_usage_of_representation
  SUBTYPE OF(class_of_relationship);
  class_of_used : class_of_representation_of_thing;
  user          : possible_individual;
END_ENTITY;
```

---

### §5.2.19 — Classes of classes of representation (p. 163–166)

#### §5.2.19.4 — `class_of_class_of_information_representation`

A `class_of_class_of_individual` that classifies information representation classes. Used when patterns themselves need to be grouped (e.g. all hex-formatted integer patterns).

```express
ENTITY class_of_class_of_information_representation
  SUPERTYPE OF (ONEOF(representation_form, language, document_definition))
  SUBTYPE OF(class_of_class_of_individual);
END_ENTITY;
```

#### §5.2.19.9 — `document_definition`

A `class_of_class_of_information_representation` that defines the **content and/or structure of documents**.

> EXAMPLE — "XYZ Corp. Material Safety Data Sheet" is a `document_definition`.

```express
ENTITY document_definition
  SUBTYPE OF(class_of_class_of_information_representation);
END_ENTITY;
```

#### §5.2.19.10 — `language`

A `class_of_class_of_information_representation` whose members are all the information representations made in the language.

> EXAMPLE — English, French, C++ and Java.

```express
ENTITY language
  SUBTYPE OF(class_of_class_of_information_representation);
END_ENTITY;
```

#### §5.2.19.11 — `representation_form`

A `class_of_class_of_information_representation` that distinguishes the form of representation.

> EXAMPLE — Hexadecimal, text, script, symbol, picture, diagram, semaphore, Morse code, music score, MIDI file format, and XML.

```express
ENTITY representation_form
  SUBTYPE OF(class_of_class_of_information_representation);
END_ENTITY;
```

---

## 3. Practical takeaways for docgraph modelling

These are *interpretations*, not part of the standard.

1. **There is no `information_carrier` class in Part 2.** The "carrier" notion appears only obliquely (the NOTE on `class_of_information_object` mentions that the carrier is usually a `physical_object`). To model the file-as-bytes layer in Part 2, classify the carrier as a `physical_object` or one of its subtypes; the relationship to the information is via `representation_of_thing` (the carrier is a `sign`).

2. **`information_object` and `information_representation` are not exposed as instance-level classes in the POSC Caesar OWL rendering** (`docs/ISO-15926-2_2003.rdf`). Only the meta-level `class_of_information_object` and `class_of_information_representation` exist. In practice this means: an actual document is modelled as an instance of `possible_individual` (or a more specific subtype like `arranged_individual`), and *classified by* an instance of `class_of_information_object`.

3. **`representation_of_thing` is a relationship class, not a thing-class.** Instances of it are reified relationship-tuples with two slots (`sign`, `represented`). The document/quote itself is a separate `possible_individual`; the "is about" link is a separate `representation_of_thing` instance connecting them.

4. **Use the right level of abstraction.** Per the NOTE in §5.2.16.4: prefer `class_of_representation_of_thing` (pattern level) for general references; use `representation_of_thing` (individual level) only when individual copies of documents are being managed.

5. **For evidence-quote modelling**, the natural shape is:
   - The quote is an `arranged_individual` (a sign).
   - A `description` instance (subtype of `representation_of_thing`) connects the quote to the entity it describes.
   - A composition relationship (e.g. `composition_of_individual`) links the quote to the parent document.
