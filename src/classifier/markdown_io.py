"""Load and save per-PDF Markdown files used as an extraction cache."""

import re
from pathlib import Path

from rich.console import Console

from .classifier import pdf_to_markdown
from .extractor import extract_pdf
from .models import ModelConfig

_STAMPS_PREFIX = "*Stamps / annotations: "
_ISSUES_PREFIX = "*Extraction issues: "
_DESC_PREFIX = "> "


def md_paths_for_pdf(pdf: Path) -> list[Path]:
    """Return all .md files saved for this PDF, sorted by index."""
    single = pdf.with_suffix(".md")
    if single.exists():
        return [single]
    return sorted(pdf.parent.glob(f"{pdf.stem}_doc*.md"))


def load_markdown(pdf: Path) -> list[dict]:
    """
    Load previously extracted Markdown documents for a PDF.
    Returns a list of dicts with keys: title, description, markdown, stamps, issues.
    """
    docs = []
    for md_path in md_paths_for_pdf(pdf):
        text = md_path.read_text(encoding="utf-8")

        issues: list[str] = []
        stamps: list[str] = []
        for prefix, bucket in ((_ISSUES_PREFIX, issues), (_STAMPS_PREFIX, stamps)):
            if text.rstrip().endswith("*") and prefix in text:
                body, footer = text.rsplit(prefix, 1)
                bucket.extend(s.strip() for s in footer.rstrip("*\n").split("|"))
                text = body.rstrip()

        title = "Document"
        description = ""
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if line.startswith("## "):
                title = line[3:].strip()
                if idx + 1 < len(lines):
                    nxt = lines[idx + 1].strip()
                    if nxt.startswith(_DESC_PREFIX):
                        description = nxt[len(_DESC_PREFIX):]
                break

        docs.append({"title": title, "description": description, "markdown": text,
                     "stamps": stamps, "issues": issues})
    return docs


def save_markdown(pdf: Path, docs: list[dict], con: Console) -> None:
    """Write extracted docs to .md file(s) and print progress."""
    if len(docs) == 1:
        _write_doc(pdf.with_suffix(".md"), docs[0])
        con.print(f"  markdown saved → [dim]{pdf.stem}.md[/dim]")
        if docs[0].get("stamps"):
            con.print(f"  stamps/annotations: [bold]{', '.join(docs[0]['stamps'])}[/bold]")
        for issue in docs[0].get("issues", []):
            con.print(f"  [yellow]extraction issue:[/yellow] {issue}")
    else:
        con.print(f"  detected [bold]{len(docs)}[/bold] sub-document(s)")
        for i, doc in enumerate(docs, 1):
            slug = re.sub(r'[^\w\-]', '_', doc["title"]).strip("_")
            md_path = pdf.with_name(f"{pdf.stem}_doc{i}_{slug}.md")
            _write_doc(md_path, doc)
            con.print(f"  [{i}] {doc['title']} → [dim]{md_path.name}[/dim]")
            if doc["stamps"]:
                con.print(f"      stamps: [bold]{', '.join(doc['stamps'])}[/bold]")
            for issue in doc.get("issues", []):
                con.print(f"      [yellow]extraction issue:[/yellow] {issue}")


def _write_doc(md_path: Path, doc: dict) -> None:
    text = f"## {doc['title']}"
    if doc.get("description"):
        text += f"\n\n{_DESC_PREFIX}{doc['description']}"
    text += f"\n\n{doc['markdown']}"
    if doc.get("stamps"):
        text += f"\n\n{_STAMPS_PREFIX}{' | '.join(doc['stamps'])}*"
    if doc.get("issues"):
        text += f"\n\n{_ISSUES_PREFIX}{' | '.join(doc['issues'])}*"
    md_path.write_text(text, encoding="utf-8")


def load_or_extract(
    pdf: Path,
    force: bool,
    client,
    model: ModelConfig,
    con: Console,
    note: str | None = None,
) -> list[dict]:
    """
    Return extracted docs for a PDF.

    Loads from cached .md files when available (unless --force).
    Calls the LLM to extract if no cache exists or force=True.
    """
    existing = md_paths_for_pdf(pdf)
    if not force and existing:
        for md_path in existing:
            con.print(f"  loading [dim]{md_path.name}[/dim]")
        return load_markdown(pdf)

    pdf_block = extract_pdf(pdf)
    con.print("  converting PDF to Markdown...")
    docs = pdf_to_markdown(pdf_block, client, model, note=note)
    save_markdown(pdf, docs, con)
    return docs
