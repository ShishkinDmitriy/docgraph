# HTML pipeline

DocGraph's source-of-truth representation of an ingested document is **HTML**,
not Markdown. This document covers the conversion → extraction → annotation
flow built on top of it.

## Why HTML

Three properties we needed and Markdown couldn't provide:

1. **Stable selectors** — every meaningful unit can carry an `id`. Citations
   are standard URL fragments (`<doc.html#id-7>`); no custom syntax, no
   text-anchored fragility.
2. **Mechanical coverage tracking** — walk the DOM, count elements with
   `[data-entity]`, report what's covered vs missed.
3. **Bi-directional navigation** — given an entity URI, find every element
   citing it; given an element, find the entities that cite it.

The token cost of HTML over Markdown (2–4× per doc) is paid only at
**conversion time** (one LLM call per ingested doc, cached). The extraction
prompts continue to consume a Markdown view rendered from the HTML —
token-efficient where it matters.

## Three artifacts, three responsibilities

```
PDF
 │
 │ (LLM call, one-shot, cached)
 ▼
canonical HTML  ── immutable, source of truth for structure + atomic units
 │
 ├──(mechanical)──> MD view  ── LLM input for extraction passes
 │
 └──(extraction + render)──> annotated HTML view  ── derived for review/coverage/exploration
```

| Artifact | Owner | Mutated by |
|---|---|---|
| Canonical HTML | conversion | nothing — immutable post-conversion |
| Markdown view | conversion derivative | regenerated on demand from canonical HTML |
| Graph (Turtle) | extraction | extraction passes only |
| Annotated HTML view | renderer | regenerated on demand from canonical + graph |

The canonical HTML is **never mutated after conversion**. All annotations
and visualizations are produced as separate artifacts. Re-extraction = drop
the graph + annotated view; canonical HTML untouched.

## Conversion

A single LLM call takes the PDF and produces one or more HTML documents.
The call's output schema:

```json
{
  "documents": [
    { "title": "...", "html": "<article>...</article>" },
    ...
  ]
}
```

A PDF that contains multiple distinct documents (invoice + receipt; article
+ appendix) is split into multiple HTML files at conversion time. Each
document gets its own URI namespace and its own extracted graph.

### HTML structure

- **Layout tags only**: `<article> <section> <header> <footer> <main> <aside>
  <h1>–<h6> <p> <ul> <ol> <li> <table> <thead> <tbody> <tr> <td> <th>
  <blockquote> <pre> <code> <em> <strong>`.
- **Banned**: `<address> <time> <dl> <dt> <dd>` and other semantic tags.
  Semantics live in the graph, not in tag choice. A renderer can substitute
  visual presentation if desired.

### IDs

The LLM assigns `id="id-N"` (sequential, mechanical) to elements that
contain **exactly one referenceable atomic unit**:
- a person's or organization's name
- an identifier (invoice number, tax ID, IBAN, BIC, etc.)
- a date or timestamp
- a quantity with unit
- a place name
- contact info (email, phone, URL)

**No IDs on**: labels (`Rechnungsnummer:`), wrappers (header/footer/
container divs), or paragraphs that contain multiple distinct objects (give
each object its own sub-span instead).

The LLM's only judgment is **which elements get an ID** (binary, well within
its training prior on document genres). The ID names themselves are
mechanical — no semantic vocabulary, no genre-specific naming. Same atomic
unit appearing in multiple places gets multiple distinct IDs; coreference
is figured out by extraction, not at conversion.

### Sub-element spans

When an atomic unit lives **inside** a larger element that holds other text,
wrap it in `<span id="id-N">` for the sub-span. Spans are inline-no-op so
selectors targeting the parent stay valid:

```html
<p>Tel.: <span id="id-13">030 676 61 84</span></p>
```

### Coreference grouping (class-N)

When the same conceptual entity appears in multiple places — a recurring
character, a person whose name appears in both header and body, an
organization referenced by name and alias — the LLM marks the coreferent
mentions with a shared `class="class-N"` token. Each mention still has its
own distinct `id="id-N"`.

```html
<span id="id-4" class="class-1">Little Red Riding Hood</span>
...
<span id="id-7" class="class-1">Little Red Riding Hood</span>
```

This gives extraction two views: per-mention precision (the ids) and
per-entity grouping (the class). The class numbering is mechanical (no
genre vocabulary). Coreference at conversion time leverages the LLM's
context — extraction doesn't have to rediscover it from text matching.

### Fragment URIs: id-N and class-N

Two kinds of fragment URIs appear in the graph:

| Fragment URI | Resolution | When |
|---|---|---|
| `<doc#id-N>` | the element with `id="id-N"` (standard URL fragment) | single-mention entity, or partial-coverage citation |
| `<doc#class-N>` | all elements with `class="class-N"` (docgraph convention) | every member of class-N is cited as evidence for the entity |

The pipeline's `html_io.collapse_anchors()` does the math: cite a class
fragment only when the entity's evidence covers ALL members of the class,
never when only a subset. Partial coverage falls back to per-id fragments.

This collapses 15-mentions-of-Grandma from 15 triples to 1, without ever
asserting class-level coverage that isn't actually backed by evidence.

### Non-text overlays (stamps, signatures, QR codes)

Things that appear on the page but aren't part of the text flow — rubber
stamps, signatures, QR codes/barcodes, watermarks, handwritten notes —
get an empty `<div>` placeholder at the position they appear in, with a
`data-note` describing what they are and (usually) an `id="id-N"` so the
graph can cite them:

```html
<header>
  <h1>Invoice 1352</h1>
  <div id="id-26" data-note="Red 'PAID' stamp diagonally across header"></div>
</header>

<p>Best regards,</p>
<div id="id-25" data-note="Handwritten signature in blue ink"></div>
<p>Dr. Smith</p>
```

The MD-view renderer surfaces these as `[OVERLAY: <description>] {#id-N}`
lines so the extraction LLM can cite them by anchor. When the stamp itself
contains text (e.g., a date stamp), put the text in the div and keep the
data-note describing the visual context.

### Example

Input: a German dental invoice PDF.

Output: a single HTML document like:

```html
<article>
  <header>
    <h1 id="id-1">Zahnarztpraxis Liebermann</h1>
    <p>Wachtelstraße <span id="id-2">17</span></p>
    <p id="id-3">12526 Berlin</p>
  </header>

  <p id="id-4">Dmitrii Shishkin</p>
  <p id="id-5">Hartriegelstr. 130 b</p>

  <table>
    <tr><td>Rechnungsnummer</td><td id="id-6">1352</td></tr>
    <tr><td>Rechnungsdatum</td><td id="id-7">17.01.2025</td></tr>
    <tr><td>Behandelte Person</td><td id="id-8">Dmitrii Shishkin</td></tr>
  </table>

  <p id="id-9">EUR 115,84</p>
</article>
```

Notes: `id-4` and `id-8` are different IDs even though both reference
"Dmitrii Shishkin". Extraction will recognize them as coreferent at typing
time (case-insensitive label match) and bind them to the same entity URI.

## Markdown view

A mechanical HTML→Markdown conversion is run after the canonical HTML is
saved. The MD view is what the extraction LLM consumes. It's regenerable
on demand and not the source of truth for anything.

The MD renderer adds an anchor marker (`{#id-N}`) after every element that
has an `id` attribute, so the LLM can echo it back when citing evidence:

```markdown
# Zahnarztpraxis Liebermann {#id-1}

Wachtelstraße 17 {#id-2}
12526 Berlin {#id-3}

Dmitrii Shishkin {#id-4}
Hartriegelstr. 130 b {#id-5}

| Rechnungsnummer | 1352 {#id-6} |
| Rechnungsdatum | 17.01.2025 {#id-7} |
| Behandelte Person | Dmitrii Shishkin {#id-8} |

EUR 115,84 {#id-9}
```

Markers are ~6 chars each — adds 50–300 tokens per doc. Trivial cost for
deterministic citation.

## Extraction

Extraction prompts are unchanged in structure. The evidence schema gains
an `anchor` field:

```json
{
  "entity": "ex:1352",
  "evidence": [{ "exact": "1352", "anchor": "id-6" }]
}
```

The pipeline:

1. Looks up `id-6` in the canonical HTML to confirm it exists.
2. Emits a graph triple: `ex:1352 lis:representedBy <doc.html#id-6>`.
3. **Does not modify the canonical HTML.**

The fragment URI is a standard URL fragment. Anything that reads HTML+RDF
can resolve it natively.

## Annotated view (derived, for humans)

`docgraph view <slug>` generates `.docgraph/annotated/<slug>.html` from
canonical HTML + graph triples. The view adds:

- `data-entity="ex:..."` attributes on elements cited by graph entities.
- `data-types="lis:Person lis:Patient"` attributes for the entity's types.
- Inline CSS for type-coloring; inline JS for hover tooltips.
- Sidebar with all entities + their connections; toggle to highlight
  uncovered sections.

The annotated view is throwaway — regenerable any time, never the source of
truth. Its purpose is review, coverage exploration, and visual debugging.

## Coverage

`docgraph coverage <slug>` walks the canonical HTML and the graph, computes:

- Elements with at least one citing entity / total elements with `id`.
- Top uncovered blocks (by `id` ranges or by structural section).
- Most-cited elements.

Mechanical, no LLM call. Output is a short diagnostic report.

## Bi-directional derivation

Both stores are independently complete enough for their natural query
shapes, and one can be regenerated from the other:

- **Graph → annotated HTML**: walk `?e lis:representedBy <doc#id>` triples,
  add `data-entity` attributes to matched elements. (This is exactly what
  `docgraph view` does.)
- **Annotated HTML → graph**: walk `[data-entity]` elements, emit
  `lis:representedBy` triples for each. Used by `docgraph reextract` if we
  ever support hand-edited annotated HTML as input.

Source of truth is the canonical HTML for structure, the graph for
semantics. Annotation links the two via fragment URIs.

## Storage layout

```
.docgraph/
  html/
    zahnrechnung2025.invoice.html       # canonical, immutable
    zahnrechnung2025.receipt.html       # second doc from the same PDF
  cache/
    zahnrechnung2025.invoice.md         # MD view (regenerable)
    zahnrechnung2025.receipt.md         # MD view (regenerable)
  graphs/
    zahnrechnung2025.invoice.extract.ttl
    zahnrechnung2025.receipt.extract.ttl
  annotated/
    zahnrechnung2025.invoice.html       # derived view (regenerable)
    zahnrechnung2025.receipt.html       # derived view (regenerable)
```

One PDF → N canonical HTML files (one per detected document) → N graphs.
The `.invoice` / `.receipt` part of the slug comes from the conversion
LLM's document split.

## Deferred decisions

The following are intentionally deferred until a real document forces them:

- **Coreference at conversion time**. Currently each mention gets its own
  ID; extraction does coreference via label match. If extraction's
  coreference becomes unreliable, conversion can mark coreferent mentions
  with a shared `class="x1"` (or analogous attribute).
- **Sub-element granularity beyond `<span>`**. Text-fragment URIs
  (`#:~:text=...`) are W3C-standard but more complex to implement; we'll
  reach for them only if a doc shape requires sub-character-level citation.
- **LLM-named semantic IDs** (`id="invoice-number"` instead of `id="id-7"`).
  Would give cross-doc queryability for free but requires a per-genre
  vocabulary. Mechanical IDs are simpler and don't preclude adding semantic
  aliases later via additional attributes.
