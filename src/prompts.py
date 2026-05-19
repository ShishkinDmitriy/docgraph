"""LLM prompt templates for PDF extraction."""

HTML_PROMPT = """Analyse this PDF and convert its content to one or more \
HTML documents.

== DOCUMENT SPLITTING ==

Decide whether the PDF contains one document or several distinct documents \
(e.g. an invoice on page 1 and a payment receipt on page 2). Split at clear \
document boundaries such as separate headers, different issuers, or page \
breaks that introduce a new document type. Do NOT split sections of the same \
document. If any page carries explicit pagination ("Page 1 of 2", "Seite 1/3", \
"1/2") treat the entire paginated sequence as one document.

Most PDFs are one document. Only emit multiple documents when the structural \
break is unambiguous.

== HTML STRUCTURE ==

Use ONLY HTML5 layout tags:
  <article> <section> <header> <footer> <main> <aside>
  <h1>–<h6> <p>
  <ul> <ol> <li>
  <table> <thead> <tbody> <tr> <td> <th>
  <blockquote> <pre> <code>
  <em> <strong>
  <span> (only for sub-element wrapping; see below)

Do NOT use semantic tags like <address>, <time>, <dl>, <dt>, <dd>, <cite>, \
<mark>. Semantics live elsewhere in the pipeline; the HTML stays purely \
structural.

== TEXT FIDELITY (critical) ==

EVERY TEXT NODE in the HTML must be VERBATIM from the PDF. You may infer \
document STRUCTURE (where a section begins, whether a line is a heading or \
a paragraph) from layout cues, but you may NOT add labels, titles, captions, \
or any other text that doesn't appear in the source document.

  BAD:
    <section>
      <h2>Recipient Information</h2>      <!-- invented label -->
      <p>Herr</p>
      <p>Dmitrii Shishkin</p>
    </section>

  GOOD (heading reflects actual source text):
    <section>
      <h2>Rechnung</h2>                   <!-- the PDF actually says this -->
      <p>Rechnungsnummer: 1352</p>
    </section>

  GOOD (no heading when the source has none):
    <section>
      <p>Herr</p>
      <p>Dmitrii Shishkin</p>
    </section>

Structural grouping with <section> is fine without a heading. Inventing a \
heading to label what a section is "about" is NOT fine — that's classification, \
which happens later in the pipeline. Your job is only the text + structure.

== INTERPRETIVE NOTES (the one allowed kind of inference) ==

The `data-note` attribute is your channel for describing what something IS \
without putting that description into the visible text. It can be attached \
to ANY element — `<section>`, `<table>`, `<tr>`, `<p>`, `<span>`, `<div>`, \
anything.

Common uses:

  Section roles (most common):
    <section data-note="Recipient information block">
      <p>Herr</p>
      <p>Dmitrii Shishkin</p>
    </section>

  Table / row purpose:
    <table data-note="Line items">...</table>
    <tr data-note="Subtotal row">...</tr>

  Per-entity descriptions (handy alongside class grouping):
    <span id="id-4" class="class-1" \
          data-note="Little Red Riding Hood — protagonist">Little Red Riding Hood</span>
    <span id="id-7" class="class-1">Little Red Riding Hood</span>

  Visual / non-text artifacts (see overlays below for empty <div>s):
    <p data-note="Logo / letterhead — embedded image">[image]</p>

Notes are OPTIONAL and may be in English even if the document is another \
language. They're hints for downstream extraction; nobody renders them as \
visible text. Use them where they help; don't note every paragraph.

When the same class-N has many mentions, one data-note on any single \
mention is usually enough — downstream tools propagate it across the group.

== NON-TEXT OVERLAYS (stamps, QR codes, signatures, handwriting) ==

Things that appear on the page but aren't part of the text flow — rubber \
stamps ("PAID", "RECEIVED", date stamps), signatures, QR codes / barcodes, \
watermarks, handwritten margin notes, logos — get an empty <div> placeholder \
at the position where they appear, with `data-note` describing what they \
are and an `id="id-N"` so the graph can cite them later.

  <p>Best regards,</p>
  <div id="id-25" data-note="Handwritten signature in blue ink"></div>
  <p>Dr. Smith</p>

  <header>
    <h1>Invoice 1352</h1>
    <div id="id-26" data-note="Red 'PAID' stamp diagonally across header"></div>
  </header>

  <footer>
    <div id="id-27" data-note="QR code, lower-right corner"></div>
  </footer>

When the stamp itself contains text (e.g., a date stamp "21.01.2026"), put \
the text in the div as `<div id="id-X" data-note="...">21.01.2026</div>` — \
text is still verbatim, the data-note describes the visual context.

The `stamps` array in the response below is a separate human-readable \
summary; the HTML divs are the positional anchors. Both should reflect the \
same set of overlays.

Preserve all text, tables, and numeric values exactly as they appear (same \
language, same casing, same punctuation, same numeric formatting). Don't \
translate. Don't normalize "115,84" to "115.84". Don't expand abbreviations. \
Don't summarize. Don't reorder.

When text is partially hidden, obscured, illegible, cut off, or covered \
(e.g. by a stamp, fold, redaction), mark the affected spot inline with \
`[UNCLEAR: <reason>]` — that's the only kind of text addition permitted.

== ID SEEDING (the only judgment we ask of you) ==

Assign `id="id-N"` (sequential, mechanical: id-1, id-2, id-3, …) to every \
element that contains EXACTLY ONE referenceable atomic unit.

**An atomic unit is any concrete, individually identifiable thing the \
document mentions** — anything that could plausibly become a node in a \
knowledge graph extracted from this document. The shape depends on the \
document's genre. Some examples across genres:

  Business / financial documents:
  - person or organization names ("Dmitrii Shishkin", "Zahnarztpraxis Liebermann")
  - identifiers (invoice number, tax ID, IBAN, BIC, order number, SKU)
  - dates and timestamps ("17.01.2025", "21.03.26 14:24 Uhr")
  - quantities with unit ("EUR 115.84", "32 kg", "850 W")
  - place names or addresses ("12526 Berlin", "Wachtelstraße 17")
  - contact info (email, phone, URL)
  - status phrases / dispositions ("umsatzsteuerfrei nach §4 Nr.14a UStG")

  Narrative / literary documents (stories, fairy tales, fiction, articles):
  - characters and named individuals ("Little Red Riding Hood", "Grandma", \
    "the Big Bad Wolf", "Wise Owl")
  - named or distinctive places ("the green door", "the forest", "Storyland")
  - significant objects / props ("the magic medicine", "the pointy shoes")
  - distinctive named events ("the wedding", "the storm of '74")

  Scientific / technical documents:
  - named methods, theorems, equations, datasets ("Welch test", "MNIST", \
    "Schrödinger equation")
  - chemical compounds, gene names, species, instruments

  Legal / contractual documents:
  - parties, defined terms, statute citations, dates of effect, monetary \
    amounts, jurisdictions, case numbers

The unifying rule: **if a downstream knowledge graph extractor would want \
to mint a separate URI for this thing, it deserves an `id`**. When in doubt \
about whether something is "important enough," err on the side of seeding \
an ID — extra anchors are cheap; missing ones can't be recovered.

Skip generic prose, narrative connective tissue, transitions, decorative \
text, and labels that are pure formatting hints (e.g. table column headers \
like "Item", "Quantity", "Total").

Do NOT assign IDs to:

  - Labels accompanying values ("Rechnungsnummer:", "Tax ID:", etc.)
  - Container elements (`<header>`, `<footer>`, `<table>`, `<tr>` rows when \
    the cells inside already carry IDs)
  - Decorative or navigational text
  - Paragraphs that contain MULTIPLE distinct atomic units (give each \
    sub-unit its own `<span id="id-N">` instead — see below)

== SUB-ELEMENT WRAPPING ==

When a referenceable unit lives INSIDE a larger element that holds OTHER \
text too, wrap just the unit in `<span id="id-N">`. The surrounding element \
itself usually has no ID:

  <p>Tel.: <span id="id-13">030 676 61 84</span></p>
  <p>IBAN: <span id="id-15">DE83300606010061538851</span></p>

Spans are layout-no-op (no visual change), so this gives finer-grained \
addressability without disturbing the structure.

== COREFERENCE ==

Two orthogonal mechanisms:

  - `id="id-N"`     — UNIQUE per mention (citation precision).
  - `class="class-N"` — SHARED across coreferent mentions (entity grouping).

EVERY id ATTRIBUTE MUST BE UNIQUE. This is an HTML specification requirement, \
not just our convention. Browsers silently drop duplicate IDs after the first.

When the SAME conceptual entity (character, person, organization, place, ...) \
appears in MULTIPLE places, give all those mentions the same `class="class-N"` \
but distinct `id="id-N"` values.

  Example 1 (business doc): "Dmitrii Shishkin" appears in the recipient \
  block AND in the "Behandelte Person" cell:
    <p><span id="id-4" class="class-1">Dmitrii Shishkin</span></p>
    ...
    <td><span id="id-9" class="class-1">Dmitrii Shishkin</span></td>
  Two distinct ids (id-4, id-9), shared class (class-1) marking them as \
  the same conceptual person.

  Example 2 (narrative): "Little Red Riding Hood" appears 14 times in the \
  story. Each mention gets a NEW id (id-1, id-7, id-12, …) and they all \
  SHARE the same class (e.g. class-3). The wolf appears as "Big Bad Wolf" \
  and "the wolf" — different surface forms, same conceptual entity, so \
  same class but different ids.

Class numbering is mechanical (class-1, class-2, …) like ids. Coin a new \
class number each time you encounter a NEW conceptual entity; reuse the \
existing class number on subsequent mentions of an already-named entity.

When an entity is mentioned only ONCE in the entire document, the class \
attribute is optional — there's nothing to group it with.

Why both mechanisms:
  - Per-mention ids let downstream tools cite the exact position ("this \
    fact came from this paragraph").
  - Per-entity classes let extraction recognize coreference in one step \
    instead of re-discovering it from text matching.

== STAMPS / ANNOTATIONS ==

Identify any visual stamps, seals, handwritten notes, or annotations on each \
document (e.g. "PAID", "RECEIVED", "APPROVED", date stamps, rubber stamps, \
signatures). Report them in the `stamps` array per document.

== RESPONSE FORMAT ==

For each document, emit the BODY CONTENT — start with an `<article>` (or a \
small set of top-level blocks like `<header>`, `<main>`, `<footer>`) and \
work inside that. Do NOT emit `<!DOCTYPE html>`, `<html>`, `<head>`, or \
`<body>` — those are added automatically by the pipeline with a CSS layer \
that visualizes IDs and data-notes for coverage review.

Respond with JSON only, no prose:

{
  "documents": [
    {
      "title": "<short human-readable title, e.g. Invoice, Receipt, Bank Statement>",
      "description": "<2-4 sentence summary covering: document type, issuer, recipient, key dates, amounts, and purpose>",
      "lang": "<BCP-47 language tag of the document's primary language: 'de' for German, 'en' for English, 'fr' for French, 'es' for Spanish, etc. Use 'und' if multiple languages are mixed without a clear primary>",
      "html": "<body content — <article>...</article> or top-level blocks; no doctype/html/head/body wrapper>",
      "stamps": ["<stamp or annotation text>", ...],
      "issues": ["<one-line description of each extraction problem>", ...]
    }
  ]
}

Rules:
- Always return at least one entry in "documents".
- Use an empty list for "stamps" and "issues" when none are found.
- Output all text as UTF-8 characters directly; do NOT use JSON Unicode \
  escape sequences (\\uXXXX).
- IDs are sequential (id-1, id-2, …) but only assigned to elements with an \
  atomic unit. The sequence stays ascending; gaps are fine if you change \
  your mind midway."""


MARKDOWN_PROMPT = """Analyse this PDF and convert its content to Markdown.

First decide whether the PDF contains one document or several distinct documents \
(e.g. an invoice on page 1 and a payment receipt on page 2).  Split at clear \
document boundaries such as separate headers, different issuers, or page breaks \
that introduce a new document type.  Do NOT split sections of the same document.

If any page carries explicit pagination (e.g. "Page 1 of 2", "Seite 1/3", "1/2") \
treat the entire paginated sequence as one document, regardless of how many pages it spans.

Also identify any visual stamps, seals, handwritten notes, or annotations on each \
document (e.g. "PAID", "RECEIVED", "APPROVED", date stamps, rubber stamps, signatures).

Whenever text is partially hidden, obscured, illegible, cut off, or covered \
(e.g. by a stamp, fold, redaction, or poor scan quality), mark the affected \
spot inline with `[UNCLEAR: <reason>]`, e.g.:
  Tel: 012 345 [UNCLEAR: last 4 digits hidden by stamp]
  IBAN: DE89 3704 [UNCLEAR: remainder cut off at page edge]

Respond with JSON only, no prose:
{
  "documents": [
    {
      "title": "<short human-readable title, e.g. Invoice, Receipt, Bank Statement>",
      "description": "<2-4 sentence summary covering: document type, issuer, recipient, key dates, amounts, and purpose — anything useful for downstream classification or data extraction>",
      "markdown": "<full content of this document as Markdown, with [UNCLEAR: reason] markers where extraction failed>",
      "stamps": ["<stamp or annotation text>", ...],
      "issues": ["<one-line description of each extraction problem, e.g. 'Phone number partially hidden by PAID stamp'>", ...]
    }
  ]
}

Rules:
- Always return at least one entry in "documents".
- Use an empty list for "stamps" and "issues" when none are found.
- Preserve all text, tables, and numeric values faithfully in "markdown".
- Output all text as UTF-8 characters directly; do NOT use JSON Unicode escape sequences (\\uXXXX)."""
