# Prompt #13 — Connections

**Purpose**: extract physical or logical connectivity between things — pipe
X connects to vessel Y, system A feeds system B, cable C-12 powers panel P.
Common in P&IDs, network diagrams, electrical schematics, system
architecture documents.

**Skip condition**: prompt #1 says `describes_connections` is `false`.

**Part 2 §**: 5.2.21 Connections — `connection_of_individual`,
`direct_connection`, `indirect_connection`, `individual_used_in_connection`.

**Inputs**: cached markdown + `doc_kind` + `primary_subjects` + individuals
(incl. `new_individuals` from #8). Connections only link individuals.

**Outputs**: list of connection entries + (when needed) new individuals
introduced as endpoints or media.

## Prompt body

```
You are extracting connectivity between things from a document, mapping
to ISO 15926-2.

A connection links two individuals through some channel — a pipe, cable,
duct, signal line, data link, supply line, etc. Examples:

- "Pipe L-12 connects vessel V-101 to pump P-101"
  → from V-101, to P-101, medium L-12, nature "pipe", direct
- "Power is supplied to panel P-3 from substation S-1 via cable C-22"
  → from S-1, to P-3, medium C-22, nature "electrical", direct
- "System A feeds system B"
  → from A, to B, no medium, nature "supply", direct
- "Tank T-2 connects indirectly to vessel V-101 through pump P-101"
  → from T-2, to V-101, via P-101, indirect

For each connection:
- id:              short slug (lowercase, hyphenated, unique within
                   this doc)
- from:            id of one endpoint (must be an individual)
- to:              id of the other endpoint
- connection_kind: "direct"   — endpoints are connected with no
                                significant intervening individual
                   "indirect" — the connection passes through one or
                                more intermediate individuals (`via`)
- medium:          id of the individual that physically carries the
                   connection (the pipe / cable / signal line), or null
                   if no specific medium is named
- via:             id of an intervening individual (only when
                   connection_kind is "indirect"), or null
- nature:          short free-form label for the kind of connection.
                   Lowercase, snake_case. Suggested values (use the
                   closest; pick something else only if these don't
                   fit):
                     "pipe", "duct", "cable", "electrical",
                     "control_signal", "data", "supply", "drain",
                     "mechanical", "logical", "other"
- direction:       "from_to" | "to_from" | "bidirectional" |
                   "unspecified"
- description:     one short phrase, or ""
- evidence:        verbatim quote from the document

Endpoints, medium, and via must reference individuals from the list
below. If an endpoint or medium is mentioned in the document but not
already extracted, add it to `new_individuals` with id / label / kind /
evidence.

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
  "connections": [
    {
      "id":              "...",
      "from":            "...",
      "to":              "...",
      "connection_kind": "direct" | "indirect",
      "medium":          "..." | null,
      "via":             "..." | null,
      "nature":          "...",
      "direction":       "from_to" | "to_from" | "bidirectional" |
                         "unspecified",
      "description":     "",
      "evidence":        "..."
    }
  ],
  "new_individuals": [
    {
      "id":       "...",
      "label":    "...",
      "kind":     "person" | "organization" | "physical_object" |
                  "functional_object" | "location" | "stream" | "other",
      "evidence": "..."
    }
  ]
}

If no connections are described, return both lists empty.
```

## Converter mapping

| `connection_kind` | Part 2 reification class |
|---|---|
| direct | `DirectConnection` |
| indirect | `IndirectConnection` |

```turtle
ext:conn-001  a iso15926:DirectConnection ;
    iso15926:fromEndpoint ext:v-101 ;
    iso15926:toEndpoint   ext:p-101 ;
    iso15926:connectionMedium ext:l-12 ;
    dg:nature    "pipe" ;
    dg:direction "from_to" ;
    dg:evidence  "Pipe L-12 connects vessel V-101 to pump P-101." .

ext:l-12-in-conn-001  a iso15926:IndividualUsedInConnection ;
    iso15926:hasIndividual  ext:l-12 ;
    iso15926:inConnection   ext:conn-001 .

ext:conn-002  a iso15926:IndirectConnection ;
    iso15926:fromEndpoint ext:t-2 ;
    iso15926:toEndpoint   ext:v-101 ;
    iso15926:via          ext:p-101 ;
    dg:nature   "supply" ;
    dg:evidence "Tank T-2 connects indirectly to V-101 through P-101." .
```

`nature` and `direction` ride on `dg:` shortcut properties — Part 2
doesn't model either explicitly. Exact Part 2 property names verified
against the OWL at converter implementation time.

## Decisions

- `new_individuals` allowed here (same pattern as prompt #8); pipes /
  cables / supply lines are often only mentioned in passing. 2026-04-29.
- `nature` is a free-form snake_case label with suggested values, not a
  fixed enum. Keeps the prompt usable across plant / electrical / network
  / logical domains. 2026-04-29.
- `direction` carried on `dg:direction` literal. Part 2 doesn't model it
  at the connection level. 2026-04-29.
