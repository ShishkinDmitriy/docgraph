"""recognize — seq 1 delta: file/doc typing + Quality-chain metadata.

Pure local work: emits dg:PdfFile + lis:PhysicalObject + dg:Document +
lis:InformationObject typing, plus file metadata (filePath / fileHash /
fileSize / mimeType / pdfProducer) as LIS-14 Quality chains. No LLM
call. See structural.py:build_recognize_graph.

Dirty check: clean iff the latest recognize delta already records the
file_uri as dg:PdfFile AND its FileHash quality chain matches
ctx["file_hash"]. Stronger than "delta exists" — catches the case
where the recorded content drifted from the actual source.
"""

from __future__ import annotations

from rdflib.namespace import RDF

from src.deltas import StepDelta, delta_path, doc_scope, next_seq, write_delta
from src.extract_part14.structural import DG, LIS, build_recognize_graph
from src.tasks._helpers import (
    latest_delta_of_step,
    now,
    print_delta_summary,
)
from src.tasks._registry import docgraph
from src.pdfinfo import pdfinfo


@docgraph.task(desc="Recognize PDF: type + file-metadata quality chain",
               deps=("identity",))
def recognize(ctx) -> None:
    console = ctx["console"]
    info = pdfinfo(ctx["path"])
    if info:
        console.print(f"  pdfinfo: [dim]{info.get('Pages', '?')} page(s), "
                      f"{info.get('Title') or '(no title)'}[/dim]")
    g = build_recognize_graph(
        file_path    = ctx["path"],
        file_uri     = ctx["file_uri"],
        doc_uri      = ctx["doc_uri"],
        project_root = ctx["project_root"],
        file_hash    = ctx["file_hash"],
        file_size    = ctx["file_size"],
        mime_type    = "application/pdf",
        pdf_info     = info,
    )
    seq = next_seq(ctx["project_root"], doc_scope(ctx["slug"]))
    write_delta(
        StepDelta(scope=doc_scope(ctx["slug"]), step="recognize", seq=seq,
                  added=g, parent_seq=seq - 1, timestamp=now()),
        delta_path(ctx["project_root"], doc_scope(ctx["slug"]), seq),
    )
    print_delta_summary(console, seq, len(g), 0)


@docgraph.dirty
def recognize_dirty(ctx) -> bool:
    if "path" not in ctx:
        return False                   # slug-based invocation — no file to recognize
    latest = latest_delta_of_step(ctx, "recognize")
    if latest is None:
        return True
    g = latest.added
    file_uri = ctx["file_uri"]
    if (file_uri, RDF.type, DG.PdfFile) not in g:
        return True
    return not _filehash_matches(g, file_uri, ctx["file_hash"])


def _filehash_matches(g, file_uri, expected_hash: str) -> bool:
    """Walk the LIS-14 FileHash quality chain on *file_uri* in *g* and
    check whether its datum value equals *expected_hash*. Only used by
    recognize_dirty, kept here so the dirty check is self-contained."""
    for q in g.objects(file_uri, LIS.hasQuality):
        if (q, RDF.type, DG.FileHash) not in g:
            continue
        for datum in g.objects(q, LIS.qualityQuantifiedAs):
            for v in g.objects(datum, LIS.datumValue):
                if str(v) == expected_hash:
                    return True
    return False
