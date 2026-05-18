"""Load / save canonical HTML documents and render the Markdown view.

The canonical HTML is produced once (PDF → HTML, LLM call) and then
**never modified** for the lifetime of the document. The Markdown view is
a derived, throwaway artifact: regenerated on demand from the HTML, used
by the extraction LLM as a token-efficient input. See
`docs/architecture/html-pipeline.md` for the full design.

This module provides:
  - `html_paths(dir)` — list converted HTML files in a doc dir
  - `save_html(docs, dir)` — write `converted[.<part>].html` files
  - `load_html(dir)` — read them back
  - `load_or_extract_html(pdf, ...)` — convert PDF→HTML if not cached, else load
  - `render_markdown_view(html_text)` — derive the LLM-input Markdown view
    with `{#id-N}` anchor markers per element that has an `id` attribute
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from rich.console import Console

from .classifier import pdf_to_html
from .extractor import extract_pdf
from .models import ModelConfig

logger = logging.getLogger(__name__)


# ── File-naming convention ────────────────────────────────────────────────

# A single PDF may yield multiple HTML documents (invoice + receipt, etc.).
# Naming inside the doc dir: `converted.html` for single-document PDFs,
# `converted.<part>.html` for multi-document. The PDF stem is NOT in the
# filename — the dir is keyed by slug, so the doc dir already disambiguates.
_SINGLE_NAME      = "converted.html"
_PART_TEMPLATE    = "converted.{part}.html"
_PART_GLOB        = "converted.*.html"


def html_paths(html_dir: Path) -> list[Path]:
    """Return all converted HTML files under *html_dir*, sorted.

    Discovers both the single-document (`converted.html`) and multi-document
    (`converted.<part>.html`) layouts.
    """
    single = html_dir / _SINGLE_NAME
    parts  = sorted(html_dir.glob(_PART_GLOB))
    out: list[Path] = []
    if single.exists():
        out.append(single)
    out.extend(p for p in parts if p != single)
    return out


def save_html(docs: list[dict], html_dir: Path, console: Console) -> list[Path]:
    """Write each extracted HTML document to disk. Returns the list of files.

    The LLM emits only the body content (e.g., `<article>...</article>`);
    save_html wraps it in a full HTML document with DOCTYPE, head, and a
    `<style>` block that visualizes IDs and data-notes. Opening the file
    in a browser shows the document with coverage highlights — referenceable
    atomic units outlined red, structural notes outlined green, overlay
    placeholders filled in.

    Single document → `converted.html`.
    Multiple documents → `converted.<slugified-title>.html` per document.
    """
    html_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if len(docs) == 1:
        path = html_dir / _SINGLE_NAME
        path.write_text(_wrap_document(docs[0]), encoding="utf-8")
        console.print(f"  wrote   [dim]{path.name}[/dim]")
        written.append(path)
        return written

    for doc in docs:
        part = _slugify(doc.get("title", "doc"))
        path = html_dir / _PART_TEMPLATE.format(part=part)
        path.write_text(_wrap_document(doc), encoding="utf-8")
        console.print(f"  wrote   [dim]{path.name}[/dim]")
        written.append(path)
    return written


_VISUALIZATION_CSS = """\
/* DocGraph canonical-HTML visualization layer.
 * - Elements with `id="id-N"` are referenceable atomic units (red).
 * - Elements with `data-note="..."` are structural inferences (green).
 * - Empty <div data-note="..."> are non-text overlays (stamps, signatures,
 *   QR codes); rendered as a labeled placeholder block.
 * The CSS layer is for human review only; CSS selectors used by extraction
 * and coverage tools target attributes, not these visual styles.
 */
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
       max-width: 900px; margin: 2em auto; padding: 0 1em; color: #222; }
article { padding: 1em; }
h1, h2, h3, h4, h5, h6 { margin-top: 1.2em; }
table { border-collapse: collapse; margin: 0.5em 0; }
th, td { padding: 4px 8px; border: 1px solid #ddd; text-align: left; }
th { background: #f0f0f0; font-weight: 600; }

/* Referenceable atomic units — outlined red, id rendered as a small tag. */
[id] {
  outline: 1px solid rgba(220, 0, 0, 0.6);
  outline-offset: 1px;
  background: rgba(220, 0, 0, 0.04);
  position: relative;
}
[id]::after {
  content: attr(id);
  font-size: 0.65em;
  background: rgba(220, 0, 0, 0.12);
  color: #c00;
  padding: 0 4px;
  margin-left: 4px;
  border-radius: 3px;
  vertical-align: super;
  white-space: nowrap;
  font-family: monospace;
}

/* Structural inferences — outlined green, note rendered above the block. */
[data-note] {
  outline: 1px dashed rgba(0, 150, 0, 0.5);
  outline-offset: 2px;
  margin: 0.5em 0;
}
[data-note]::before {
  content: "📝 " attr(data-note);
  display: block;
  font-size: 0.75em;
  color: #060;
  font-style: italic;
  margin-bottom: 0.3em;
}

/* Empty overlay divs (stamps, signatures, QR codes) — give them a visible
 * box even though they have no text content. */
div[data-note]:empty {
  display: block;
  min-height: 1.5em;
  background: rgba(0, 150, 0, 0.08);
  padding: 0.5em;
}

/* Coreference groups — same class-N → matching colored underline. We can't
 * generate a per-class color from CSS alone, so use a thicker left-border
 * accent for any element with a class-N value. Hovering one highlights all
 * group members via the [class*="class-"] selector (browser highlights all
 * mentions sharing the same class). */
[class*="class-"] {
  border-bottom: 2px dotted rgba(0, 0, 200, 0.5);
}
[class*="class-"]:hover ~ [class*="class-"],
[class*="class-"]:hover {
  background: rgba(0, 100, 255, 0.12);
}

/* Hover lifts ID-bearing or noted elements — light yellow background. */
[id]:hover, [data-note]:hover { background: rgba(255, 230, 0, 0.18); }
"""


def _wrap_document(doc: dict) -> str:
    """Wrap the LLM's body content into a full HTML document with DOCTYPE,
    head (charset + title + visualization CSS), and body. The body content
    itself is canonical — the wrapper is presentation only.

    The `lang` attribute is set from the LLM's `lang` field (BCP 47 tag
    like `de`, `en`, `fr`); falls back to `und` ("undetermined") when the
    LLM doesn't supply one.
    """
    title = doc.get("title", "Document")
    lang  = (doc.get("lang") or "und").strip() or "und"
    body  = doc.get("html", "")
    # Defensive: if the LLM emitted a full document despite the prompt,
    # strip the wrapper so our visualization layer takes over.
    body = _strip_outer_html(body)
    return (
        "<!DOCTYPE html>\n"
        f"<html lang=\"{_html_escape(lang)}\">\n"
        "<head>\n"
        "<meta charset=\"UTF-8\">\n"
        f"<title>{_html_escape(title)}</title>\n"
        "<style>\n"
        f"{_VISUALIZATION_CSS}"
        "</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _strip_outer_html(body: str) -> str:
    """If the body contains its own <html>...</html> wrapper, extract just
    the inner <body> content (or fall back to the original if no wrapper
    is detected)."""
    m = re.search(r"<body[^>]*>(.*?)</body>", body, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return body.strip()


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))


def load_html(html_dir: Path) -> list[dict]:
    """Load previously-saved HTML documents from *html_dir*. The saved files
    include the visualization wrapper (DOCTYPE, head, style); this loader
    returns just the body content so downstream consumers (the MD-view
    renderer, the extraction LLM) see the canonical content directly.

    Title and description are recomputed from the HTML's `<title>` /
    first heading if needed (not preserved on disk).
    """
    docs: list[dict] = []
    for path in html_paths(html_dir):
        full_html = path.read_text(encoding="utf-8")
        body      = _strip_outer_html(full_html)
        docs.append({
            "title": _extract_title(full_html) or _extract_title(body) or path.stem,
            "description": "",
            "html": body,
            "stamps": [],
            "issues": [],
        })
    return docs


def load_or_extract_html(
    pdf: Path,
    *,
    force: bool,
    client,
    model: ModelConfig,
    con: Console,
    note: str | None,
    html_dir: Path,
) -> list[dict]:
    """Return HTML docs for *pdf*, converting via LLM only if not cached.

    *force*=True drops the cache first and re-converts.
    """
    if force:
        for path in html_paths(html_dir):
            path.unlink()
            con.print(f"  [dim]dropped cached [dim]{path.name}[/dim][/dim]")

    cached = load_html(html_dir)
    if cached:
        con.print(f"  loading {', '.join(p.name for p in html_paths(html_dir))}")
        return cached

    pdf_block = extract_pdf(pdf)
    docs = pdf_to_html(pdf_block, client, model, note=note)
    if not docs:
        return []
    save_html(docs, html_dir, con)
    return docs


# ── Markdown view derivation (HTML → MD with {#id-N} markers) ─────────────

_TAG_RX   = re.compile(r"<\s*(/?)([a-zA-Z][a-zA-Z0-9]*)\s*([^>]*)>", re.DOTALL)
# Attribute patterns use a backreference for the closing quote so apostrophes
# inside double-quoted values (and vice versa) don't truncate the capture.
# Captured value is group 2.
_ID_RX    = re.compile(r"\bid\s*=\s*(['\"])(.*?)\1")
_NOTE_RX  = re.compile(r"\bdata-note\s*=\s*(['\"])(.*?)\1")
_CLASS_RX = re.compile(r"\bclass\s*=\s*(['\"])(.*?)\1")
_CLASS_N_RX = re.compile(r"\bclass-\d+\b")


def build_class_maps(html_text: str) -> tuple[dict[str, str], dict[str, set[str]]]:
    """Walk *html_text* and return (id_to_class, class_to_ids) maps.

    Only `class-N` tokens are tracked — other classes are ignored. An
    element with both `id="id-7"` and `class="class-4 something-else"`
    becomes `id_to_class["id-7"] = "class-4"` and `class_to_ids["class-4"]
    contains "id-7"`.

    Used by `walk_roots` to collapse evidence citations: when an entity's
    cited ids fully cover the membership of a class-N group, the graph
    emits one `lis:representedBy <doc#class-N>` instead of N per-id
    triples.
    """
    id_to_class: dict[str, str] = {}
    class_to_ids: dict[str, set[str]] = {}
    for m in _TAG_RX.finditer(html_text):
        if m.group(1) == "/":
            continue
        attrs = m.group(3) or ""
        id_m  = _ID_RX.search(attrs)
        cls_m = _CLASS_RX.search(attrs)
        if id_m is None or cls_m is None:
            continue
        element_id = id_m.group(2)
        cls_match = _CLASS_N_RX.search(cls_m.group(2))
        if cls_match is None:
            continue
        cls = cls_match.group(0)
        id_to_class[element_id] = cls
        class_to_ids.setdefault(cls, set()).add(element_id)
    return id_to_class, class_to_ids


def collapse_anchors(
    cited_ids:    set[str],
    id_to_class:  dict[str, str],
    class_to_ids: dict[str, set[str]],
) -> list[str]:
    """Return the minimal fragment list (as strings, no '#' prefix) that
    represents the *cited_ids* set.

    For each class-N group whose membership is FULLY covered by cited_ids,
    emit a single `class-N` fragment (instead of N per-id fragments). For
    cited ids not part of a fully-covered class, emit per-id fragments.

    Partial coverage (entity cites some but not all members of a class)
    falls back to per-id enumeration — never lies about coverage via a
    class fragment that includes uncited members.

    Result is sorted for stability.
    """
    out: set[str] = set()
    covered_ids: set[str] = set()

    # Group cited ids by their class (if any).
    by_class: dict[str, set[str]] = {}
    unclassed: set[str] = set()
    for i in cited_ids:
        c = id_to_class.get(i)
        if c is None:
            unclassed.add(i)
        else:
            by_class.setdefault(c, set()).add(i)

    # Fully-covered classes → single class fragment.
    for c, ids in by_class.items():
        if class_to_ids.get(c) == ids:
            out.add(c)
            covered_ids.update(ids)

    # Cited ids that didn't end up in a fully-covered class → per-id.
    out.update(unclassed)
    for ids in by_class.values():
        for i in ids - covered_ids:
            out.add(i)

    return sorted(out)


def _format_marker(element_id: str | None, css_class: str | None) -> str:
    """Build the inline anchor marker for the MD view.

    Examples:
        id only           → " {#id-4}"
        id + class        → " {#id-4 .class-1}"
        class only        → " {.class-1}"   (rare; group member without own id)
        neither           → ""              (no marker emitted)

    Classes are space-separated in HTML; we surface only the first
    `class-N` token (the coreference grouping). Other classes, if any,
    aren't relevant to the MD view.
    """
    if not element_id and not css_class:
        return ""
    parts: list[str] = []
    if element_id:
        parts.append(f"#{element_id}")
    if css_class:
        for tok in css_class.split():
            if tok.startswith("class-"):
                parts.append(f".{tok}")
                break
    if not parts:
        return ""
    return " {" + " ".join(parts) + "}"


def render_markdown_view(html_text: str) -> str:
    """Convert canonical HTML to a Markdown view annotated with `{#id-N}`
    markers after each element that carries an `id` attribute.

    Deliberately simple: targets the layout-only HTML our converter produces
    (h1-h6, p, ul/ol/li, table/tr/td, blockquote, span). Not a general-purpose
    HTML→MD library — written to know exactly what tags appear and what
    Markdown shape we want for the LLM. Anchor markers are appended inline
    to the text rendered for the element that has the id.

    Example:
        <p id="id-3">12526 Berlin</p>
            → "12526 Berlin {#id-3}\\n\\n"

        <p>Tel.: <span id="id-13">030 676 61 84</span></p>
            → "Tel.: 030 676 61 84 {#id-13}\\n\\n"
    """
    return _Renderer().render(html_text)


class _Renderer:
    """Stateful HTML→MD walker. Mutable list of buffered output chunks."""

    # Tags whose text we render but whose own boundaries don't add MD syntax.
    # `div` is handled separately — when it's an overlay placeholder (has
    # `data-note`), we emit a visible marker so extraction can reference it.
    _PASSTHROUGH = {"article", "section", "header", "footer", "main", "aside",
                    "thead", "tbody"}
    # Tags rendered as headings — depth from tag name.
    _HEADINGS    = {f"h{n}": n for n in range(1, 7)}
    # Inline tags — rendered without block break, but we do append id markers.
    _INLINE      = {"span", "em", "strong", "code"}

    def __init__(self) -> None:
        self.out: list[str] = []
        self._list_stack: list[str] = []   # "ul" or "ol" entries
        self._list_index: list[int] = []   # 1-based index within current ol
        self._in_table = False
        # Per cell in current table row: (text, id, class).
        self._row_cells: list[tuple[str, str | None, str | None]] = []
        # Stack of (tag, element_id, data_note, css_class) for currently-open
        # elements. Closing tags don't carry their attributes in HTML syntax —
        # we recall them from here.
        self._open_stack: list[tuple[str, str | None, str | None, str | None]] = []

    def render(self, html: str) -> str:
        # Normalize whitespace inside tags but preserve content order.
        # We tokenize: a sequence of (kind, value, attrs) where kind in
        # {"open", "close", "self", "text"}.
        tokens = list(self._tokenize(html))
        for tok in tokens:
            self._emit(tok)
        text = "".join(self.out)
        # Two-stage whitespace cleanup, robust against whitespace
        # leaking out of inter-tag text runs:
        #   1. Strip trailing whitespace from every line — those ` \n`
        #      artifacts come from text tokens like `\n \n` that
        #      `_normalize_inline` collapsed to a single space.
        #   2. Collapse runs of blank lines to exactly one blank line —
        #      keeps a paragraph break, drops noisy multi-blanks.
        lines = [l.rstrip() for l in text.split("\n")]
        out_lines: list[str] = []
        prev_blank = False
        for line in lines:
            if line == "":
                if not prev_blank:
                    out_lines.append(line)
                prev_blank = True
            else:
                out_lines.append(line)
                prev_blank = False
        return "\n".join(out_lines).strip() + "\n"

    # ── tokenization ──

    def _tokenize(self, html: str):
        """Yield tokens as (kind, tag_or_text, element_id, data_note, css_class).

        - data_note: captured from `data-note="..."` (interpretive metadata).
        - css_class: captured from `class="..."` (coreference grouping via
          `class-N` tokens).
        """
        pos = 0
        for m in _TAG_RX.finditer(html):
            if m.start() > pos:
                yield ("text", html[pos:m.start()], None, None, None)
            slash, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
            id_match    = _ID_RX.search(attrs or "")
            note_match  = _NOTE_RX.search(attrs or "")
            class_match = _CLASS_RX.search(attrs or "")
            element_id = id_match.group(2)    if id_match    else None
            data_note  = note_match.group(2)  if note_match  else None
            css_class  = class_match.group(2) if class_match else None
            if slash == "/":
                yield ("close", tag, element_id, data_note, css_class)
            elif (attrs or "").rstrip().endswith("/"):
                yield ("self", tag, element_id, data_note, css_class)
            else:
                yield ("open", tag, element_id, data_note, css_class)
            pos = m.end()
        if pos < len(html):
            yield ("text", html[pos:], None, None, None)

    # ── per-token emission ──

    def _emit(self, tok):
        kind, value, element_id, data_note, css_class = tok
        if kind == "text":
            self._emit_text(value)
            return
        tag = value
        if kind == "open":
            self._open_stack.append((tag, element_id, data_note, css_class))
            self._emit_open(tag, element_id, data_note, css_class)
        elif kind == "close":
            # Recall attrs from the matching open. Pop the most recent
            # matching tag (HTML may be slightly malformed; tolerate by
            # walking back).
            recalled_id, recalled_note, recalled_class = None, None, None
            for i in range(len(self._open_stack) - 1, -1, -1):
                if self._open_stack[i][0] == tag:
                    _, recalled_id, recalled_note, recalled_class = self._open_stack[i]
                    self._open_stack.pop(i)
                    break
            self._emit_close(tag, recalled_id, recalled_note, recalled_class)
        else:  # self-closing
            self._emit_open(tag, element_id, data_note, css_class)
            self._emit_close(tag, element_id, data_note, css_class)

    # ── tag-specific handling ──

    def _emit_open(self, tag: str, element_id: str | None,
                   data_note: str | None = None,
                   css_class: str | None = None) -> None:
        if tag == "div":
            # Overlay placeholder — render as a visible OVERLAY marker so
            # the LLM can cite it. data-note is the description; id-N (when
            # present) is the citation anchor.
            if data_note or element_id:
                marker = _format_marker(element_id, css_class)
                desc   = data_note or "overlay"
                self.out.append(f"\n\n[OVERLAY: {desc}]{marker}")
            return
        if tag in self._PASSTHROUGH:
            return
        if tag in self._HEADINGS:
            self.out.append("\n\n" + ("#" * self._HEADINGS[tag]) + " ")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag == "ul":
            self._list_stack.append("ul")
            self._list_index.append(1)
            self.out.append("\n\n")
        elif tag == "ol":
            self._list_stack.append("ol")
            self._list_index.append(1)
            self.out.append("\n\n")
        elif tag == "li":
            indent = "  " * (len(self._list_stack) - 1)
            kind = self._list_stack[-1] if self._list_stack else "ul"
            if kind == "ul":
                self.out.append(f"\n{indent}- ")
            else:
                idx = self._list_index[-1]
                self.out.append(f"\n{indent}{idx}. ")
                self._list_index[-1] = idx + 1
        elif tag == "table":
            self._in_table = True
            self.out.append("\n\n")
        elif tag == "tr":
            self._row_cells = []
        elif tag in ("td", "th"):
            # Capture text + optional id/class into the row buffer; emit at </tr>.
            self._row_cells.append(("", element_id, css_class))
        elif tag == "br":
            self.out.append("  \n")
        # Inline tags: span/em/strong/code — text emitted on close (so we know id binding).

    def _emit_close(self, tag: str, element_id: str | None,
                    data_note: str | None = None,
                    css_class: str | None = None) -> None:
        if tag == "div":
            # Close of an overlay marker. If the div had inline text content
            # (a stamp containing text, like "PAID 2025-01-21"), it's already
            # been emitted; we just close the block.
            self.out.append("\n")
            return
        if tag in self._PASSTHROUGH:
            return
        marker = _format_marker(element_id, css_class)
        if tag in self._HEADINGS:
            self.out.append(marker)
            self.out.append("\n")
        elif tag == "p":
            self.out.append(marker)
            self.out.append("\n")
        elif tag == "blockquote":
            self.out.append(marker)
            self.out.append("\n")
        elif tag == "ul" or tag == "ol":
            if self._list_stack:
                self._list_stack.pop()
                self._list_index.pop()
            self.out.append("\n")
        elif tag == "li":
            self.out.append(marker)
        elif tag == "table":
            self._in_table = False
            self.out.append("\n")
        elif tag == "tr":
            cells = "| " + " | ".join(
                f"{txt}{_format_marker(cid, cclass)}" if (cid or cclass) else txt
                for txt, cid, cclass in self._row_cells
            ) + " |"
            self.out.append("\n" + cells)
            self._row_cells = []
        elif tag in ("td", "th"):
            # Append id/class annotation to the just-finished cell.
            if self._row_cells and (element_id or css_class):
                if self._row_cells[-1][1] is None and self._row_cells[-1][2] is None:
                    txt, _, _ = self._row_cells[-1]
                    self._row_cells[-1] = (txt, element_id, css_class)
        elif tag == "em":
            self._emit_chunk("*")
        elif tag == "strong":
            self._emit_chunk("**")
        elif tag == "code":
            self._emit_chunk("`")
        elif tag == "span":
            self._emit_chunk(marker)

    def _emit_text(self, text: str) -> None:
        # Inside a table row: append text to the current cell's buffer.
        # Between cells (`<tr>` open but no `<td>` yet, or `<table>` open but
        # no `<tr>`) the inter-tag whitespace would otherwise leak into
        # self.out and trail off the previous row — drop it.
        if self._in_table:
            if self._row_cells:
                txt, cid, cclass = self._row_cells[-1]
                self._row_cells[-1] = (txt + _normalize_inline(text), cid, cclass)
            return
        self.out.append(_normalize_inline(text))

    def _emit_chunk(self, chunk: str) -> None:
        """Route an inline-tag close emission (markers, `*`, `**`, `` ` ``).
        In table-row mode goes into the current cell's text buffer so it
        renders inside the cell, not after the row. Otherwise self.out."""
        if self._in_table and self._row_cells:
            txt, cid, cclass = self._row_cells[-1]
            self._row_cells[-1] = (txt + chunk, cid, cclass)
        else:
            self.out.append(chunk)


def _normalize_inline(text: str) -> str:
    """Collapse whitespace within a text run while preserving meaningful spaces."""
    # Decode common HTML entities our converter may emit.
    text = (text.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&apos;", "'")
                .replace("&nbsp;", " "))
    # Collapse runs of whitespace (incl. newlines) into single spaces.
    return re.sub(r"\s+", " ", text)


def _extract_title(html: str) -> str | None:
    """Pull the document title for `load_html`.

    Prefers `<title>` in the head (set by our wrapper), falls back to the
    first `<h1>` in the body, then None.
    """
    for pattern in (r"<title[^>]*>(.*?)</title>",
                    r"<h1[^>]*>(.*?)</h1>"):
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            raw = re.sub(r"<[^>]+>", "", m.group(1))
            cleaned = _normalize_inline(raw).strip()
            if cleaned:
                return cleaned
    return None


def _slugify(s: str) -> str:
    """Filesystem-friendly slug for multi-document HTML filenames."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:48] or "doc"
