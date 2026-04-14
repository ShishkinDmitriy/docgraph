"""LLM prompt templates for PDF extraction."""

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
