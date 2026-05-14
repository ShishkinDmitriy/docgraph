"""Per-project extension classes — LLM-proposed classes anchored under
stable LIS-14 superclasses.

The mega-walker is allowed to PROPOSE a new class when it sees an entity
that doesn't fit any existing LIS-14 or already-known ext: class. Each
proposal carries:

  - `rdf:type owl:Class` + `rdfs:subClassOf <anchor>` where `<anchor>` is
    one of `ALLOWED_ANCHORS` (whitelisted LIS-14 classes).
  - `rdfs:label` (canonical name).
  - `skos:altLabel` (alternates / synonyms).
  - `rdfs:comment` (natural-language definition).
  - `dg:provenance` (status: "proposed-by-llm" or, after curation,
    "promoted" / "approved").
  - `dg:firstSeenIn` (source URI for audit).

Three-tier storage:
  1. **Per-doc extract graph** — the proposing doc's `<slug>.extract.ttl`
     contains the class definition alongside the instance triples. The
     doc graph is self-contained.
  2. **Project-wide `.docgraph/ontologies/ext.ttl`** — populated by an
     explicit `docgraph promote-classes` step that finds classes used
     across N docs, normalizes labels/comments, writes a single canonical
     definition. Loaded by the loader as another foundational.
  3. **External RDL** — reached via enrich (POSC Caesar, etc.).

This module provides:
  - `ExtClass` dataclass.
  - `normalize_slug` for stable URI naming under varied surface forms.
  - `class_definitions_graph(classes)` — emit triples (used by the
    materializer to add definitions to a per-doc extract graph).
  - `extract_classes_from_graph(graph)` — read existing ext classes back
    from any graph (for the "existing classes" list shown to the LLM).
  - `merge_proposals(existing, proposals)` — reuse-before-mint policy.
  - `ALLOWED_ANCHORS` — the LLM's permitted superclass set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SKOS


EXT = Namespace("http://example.org/docgraph/ext#")
DG  = Namespace("http://example.org/docgraph/meta#")
LIS = Namespace("http://rds.posccaesar.org/ontology/lis14/rdl/")


# ── Anchor blacklist ─────────────────────────────────────────────────────

# LIS-14 classes the LLM MAY NOT use as direct superclasses for proposed
# ext: classes. Anything else in the LIS namespace (and `dg:extractable`)
# is fair game. Blacklist policy: only block over-abstract roots whose
# subclasses split into more meaningful kinds — extensions should land at
# the next level down, where they actually distinguish something.
BLACKLISTED_ANCHORS: set[URIRef] = {
    LIS.Object,    # use Person, Organization, PhysicalObject, … instead
    LIS.Aspect,    # use Quality, Function, Disposition, Role, …
}


def is_allowed_anchor(uri: URIRef) -> bool:
    """True if *uri* is a permitted superclass for a proposed ext: class."""
    return uri not in BLACKLISTED_ANCHORS


# ── Data structure ───────────────────────────────────────────────────────

@dataclass
class ExtClass:
    """One LLM-proposed extension class."""
    slug:         str                                     # local-name (URI tail)
    anchor:       URIRef                                  # rdfs:subClassOf target
    label:        str                                     # rdfs:label canonical
    alt_labels:   list[str]      = field(default_factory=list)   # skos:altLabel
    comment:      str            = ""                            # rdfs:comment
    provenance:   str            = "proposed-by-llm"             # dg:provenance status
    first_seen:   URIRef | None  = None                          # dg:firstSeenIn source

    @property
    def uri(self) -> URIRef:
        return EXT[self.slug]


# ── Slug normalization ───────────────────────────────────────────────────

_SLUG_NORMALIZE_RX = re.compile(r"[^a-zA-Z0-9]+")


def normalize_slug(raw: str) -> str:
    """Normalize an LLM-proposed class name to a stable URI slug.

    Strips non-alphanumerics. Used so varying surface forms ("IBAN" vs
    "I.B.A.N." vs "iban_id") all collapse to the same slug for reuse.
    """
    cleaned = _SLUG_NORMALIZE_RX.sub("", raw)
    return cleaned or "Unnamed"


# ── Triple emission (for inclusion in per-doc extract graph) ─────────────

def class_definitions_graph(classes: list[ExtClass] | dict[str, ExtClass]) -> Graph:
    """Return a Graph containing rdfs class declarations for *classes*.

    The materializer adds these triples to the per-doc extract graph so
    the doc is self-contained. Promotion to project-wide `ext.ttl` is a
    separate, deliberate step.
    """
    if isinstance(classes, dict):
        items = list(classes.values())
    else:
        items = list(classes)

    g = Graph()
    g.bind("ext",  EXT)
    g.bind("dg",   DG)
    g.bind("lis",  LIS)
    g.bind("owl",  OWL)
    g.bind("rdfs", RDFS)
    g.bind("skos", SKOS)

    # Sort by slug for deterministic serialization order across runs.
    for c in sorted(items, key=lambda x: x.slug):
        uri = c.uri
        g.add((uri, RDF.type, OWL.Class))
        g.add((uri, RDFS.subClassOf, c.anchor))
        g.add((uri, RDFS.label, Literal(c.label)))
        for alt in c.alt_labels:
            g.add((uri, SKOS.altLabel, Literal(alt)))
        if c.comment:
            g.add((uri, RDFS.comment, Literal(c.comment)))
        if c.provenance:
            g.add((uri, DG.provenance, Literal(c.provenance)))
        if c.first_seen is not None:
            g.add((uri, DG.firstSeenIn, c.first_seen))
    return g


# ── Triple consumption (for the "existing classes" prompt list) ──────────

def extract_classes_from_graph(graph: Graph) -> dict[str, ExtClass]:
    """Read ext: classes already declared anywhere in *graph*, keyed by slug.

    Used to feed the mega-walker prompt with "here are the classes that
    already exist; prefer to reuse before proposing new ones." The graph
    can be the union view of the project (per-doc graphs + ext.ttl); all
    ext: declarations across docs accumulate here.

    For duplicate declarations of the same slug across docs, this loader
    is forgiving: it picks the longest non-empty label / comment as
    canonical and unions altLabels.
    """
    classes: dict[str, ExtClass] = {}
    for cls_uri in graph.subjects(RDF.type, OWL.Class):
        if not isinstance(cls_uri, URIRef):
            continue
        if not str(cls_uri).startswith(str(EXT)):
            continue
        slug = str(cls_uri)[len(str(EXT)):]
        anchor = next((o for o in graph.objects(cls_uri, RDFS.subClassOf)
                       if isinstance(o, URIRef)), None)
        if anchor is None:
            continue

        labels = sorted({str(o) for o in graph.objects(cls_uri, RDFS.label)},
                        key=len, reverse=True)
        comments = sorted({str(o) for o in graph.objects(cls_uri, RDFS.comment)},
                          key=len, reverse=True)
        alt_labels = sorted({str(o) for o in graph.objects(cls_uri, SKOS.altLabel)})
        provenance = next((str(o) for o in graph.objects(cls_uri, DG.provenance)),
                          "proposed-by-llm")
        first_seen = next((o for o in graph.objects(cls_uri, DG.firstSeenIn)
                           if isinstance(o, URIRef)), None)

        existing = classes.get(slug)
        if existing is None:
            classes[slug] = ExtClass(
                slug       = slug,
                anchor     = anchor,
                label      = labels[0] if labels else slug,
                alt_labels = alt_labels,
                comment    = comments[0] if comments else "",
                provenance = provenance,
                first_seen = first_seen,
            )
            continue

        # Merge with existing entry (multi-doc case): union altLabels,
        # longest comment wins, longest label wins, prefer first-seen.
        merged_alts = sorted(set(existing.alt_labels) | set(alt_labels))
        if existing.label not in merged_alts and labels and labels[0] != existing.label:
            merged_alts.append(existing.label)
        existing.alt_labels = merged_alts
        if labels and len(labels[0]) > len(existing.label):
            existing.label = labels[0]
        if comments and len(comments[0]) > len(existing.comment):
            existing.comment = comments[0]
    return classes


# ── Reuse-before-mint policy ─────────────────────────────────────────────

def merge_proposals(
    existing: dict[str, ExtClass],
    proposals: list[ExtClass],
) -> tuple[dict[str, ExtClass], list[ExtClass]]:
    """Merge *proposals* into *existing*; return (merged, newly-added).

    For each proposal:
      - If a class with the same slug already exists, MERGE alt_labels
        (union) and skip re-mining the comment/anchor (existing wins).
        Treated as "already known"; not in newly-added.
      - Otherwise, add it. Counts as newly-added (gets emitted as triples
        in the per-doc extract graph).
    """
    merged = dict(existing)
    newly: list[ExtClass] = []
    for p in proposals:
        if p.slug in merged:
            existing_cls = merged[p.slug]
            for alt in p.alt_labels:
                if alt not in existing_cls.alt_labels:
                    existing_cls.alt_labels.append(alt)
            # Add the proposal's surface label as an alt if it differs
            if p.label != existing_cls.label and p.label not in existing_cls.alt_labels:
                existing_cls.alt_labels.append(p.label)
            continue
        merged[p.slug] = p
        newly.append(p)
    return merged, newly
