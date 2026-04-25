"""Project root discovery and .docgraph directory initialisation.

Layout (per ARCHITECTURE.md):

    .docgraph/
      meta.ttl           — docgraph extensions, owl:imports the upper ontologies
      lis-14.ttl         — ISO 15926 Part 14 upper ontology
      prov-o.ttl         — W3C PROV-O (provenance)
      dcterms.ttl        — DCMI Terms (bibliographic metadata)
      sources.ttl        — registry of ingested sources
      graphs/
        _unresolved.ttl  — stubs for not-yet-defined concepts
        <slug>.ttl       — one file per source (real file or symlink to TTL input)
      cache/             — PDF→Markdown cache (unchanged)
"""

import shutil
from pathlib import Path

from rich.console import Console

DOCGRAPH_DIR        = ".docgraph"
META_FILENAME       = "meta.ttl"
LIS14_FILENAME      = "lis-14.ttl"
PROV_O_FILENAME     = "prov-o.ttl"
DCTERMS_FILENAME    = "dcterms.ttl"
SOURCES_FILENAME    = "sources.ttl"
GRAPHS_SUBDIR       = "graphs"
UNRESOLVED_FILENAME = "_unresolved.ttl"
CACHE_SUBDIR        = "cache"

# Bundled upper-ontology sources.
_DOCS_DIR     = Path(__file__).parent.parent / "docs"
_LIS14_SOURCE   = _DOCS_DIR / "LIS-14.ttl"
_PROV_O_SOURCE  = _DOCS_DIR / "prov-o.ttl"
_DCTERMS_SOURCE = _DOCS_DIR / "dcterms.ttl"

_BUNDLED_ONTOLOGIES = [
    (_LIS14_SOURCE,   LIS14_FILENAME,   "ISO 15926 Part 14"),
    (_PROV_O_SOURCE,  PROV_O_FILENAME,  "W3C PROV-O"),
    (_DCTERMS_SOURCE, DCTERMS_FILENAME, "DCMI Terms"),
]


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for a directory that contains
    ``.docgraph/sources.ttl``. Returns the project root or None."""
    current = (start or Path.cwd()).resolve()
    while True:
        if (current / DOCGRAPH_DIR / SOURCES_FILENAME).is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def docgraph_dir(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR


def meta_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / META_FILENAME


def lis14_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / LIS14_FILENAME


def prov_o_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / PROV_O_FILENAME


def dcterms_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / DCTERMS_FILENAME


def sources_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / SOURCES_FILENAME


def graphs_dir(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / GRAPHS_SUBDIR


def unresolved_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / GRAPHS_SUBDIR / UNRESOLVED_FILENAME


def cache_dir(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / CACHE_SUBDIR


_META_TTL = """\
@prefix dg:      <http://example.org/docgraph/meta#> .
@prefix lis:     <http://standards.iso.org/iso/15926/part14/> .
@prefix prov:    <http://www.w3.org/ns/prov#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix owl:     <http://www.w3.org/2002/07/owl#> .
@prefix rdf:     <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:    <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .

<http://example.org/docgraph/meta>  a owl:Ontology ;
    rdfs:label   "DocGraph meta-ontology" ;
    rdfs:comment "Extensions on top of ISO 15926 Part 14, PROV-O, and DCMI Terms." ;
    owl:imports  <http://standards.iso.org/iso/15926/part14> ,
                 <http://www.w3.org/ns/prov-o-20130430> ,
                 <http://purl.org/dc/terms/> .

# ── Light alignment between Part 14 and PROV-O ────────────────────────────────
# (prov:Agent is intentionally not aligned — it spans Person, Organization, SoftwareAgent.)
prov:Activity  rdfs:subClassOf lis:Activity .
prov:Entity    rdfs:subClassOf lis:Object .

# ── Modality (RFC 2119 / ISO drafting directives) ─────────────────────────────
dg:Modality   a owl:Class ;
    rdfs:label "Modality" .

dg:Mandatory  a dg:Modality ; rdfs:label "Mandatory"  .  # MUST / SHALL
dg:Preferred  a dg:Modality ; rdfs:label "Preferred"  .  # SHOULD
dg:Optional   a dg:Modality ; rdfs:label "Optional"   .  # MAY
dg:Prohibited a dg:Modality ; rdfs:label "Prohibited" .  # MUST NOT

dg:modality   a owl:ObjectProperty ;
    rdfs:label "modality" ;
    rdfs:range  dg:Modality .

# ── Document subject — what an InformationObject is about ─────────────────────
dg:isAbout    a owl:ObjectProperty ;
    rdfs:label  "isAbout" ;
    rdfs:domain lis:InformationObject ;
    rdfs:range  owl:Class .

# ── File metadata ─────────────────────────────────────────────────────────────
dg:filePath    a owl:DatatypeProperty ; rdfs:label "filePath"    ; rdfs:range xsd:string  .
dg:fileHash    a owl:DatatypeProperty ; rdfs:label "fileHash"    ; rdfs:range xsd:string  .  # "sha256:..."
dg:fileSize    a owl:DatatypeProperty ; rdfs:label "fileSize"    ; rdfs:range xsd:integer .  # bytes
dg:mimeType    a owl:DatatypeProperty ; rdfs:label "mimeType"    ; rdfs:range xsd:string  .
dg:pageCount   a owl:DatatypeProperty ; rdfs:label "pageCount"   ; rdfs:range xsd:integer .
dg:pdfProducer a owl:DatatypeProperty ; rdfs:label "pdfProducer" ; rdfs:range xsd:string  .

# ── Conversion / activity I/O (subproperties of PROV's used / generated) ──────
dg:hasInput   a owl:ObjectProperty ;
    rdfs:label  "hasInput" ;
    rdfs:subPropertyOf prov:used .

dg:hasOutput  a owl:ObjectProperty ;
    rdfs:label  "hasOutput" ;
    rdfs:subPropertyOf prov:generated .

# ── Stub status (for graphs/_unresolved.ttl) ──────────────────────────────────
dg:status     a owl:ObjectProperty ;
    rdfs:label "status" .

dg:Unresolved a owl:NamedIndividual ;
    rdfs:label "Unresolved" .

dg:firstSeenIn a owl:ObjectProperty ;
    rdfs:label "firstSeenIn" .

# ── Ingestion registry vocabulary (used in sources.ttl) ───────────────────────
dg:IngestionRecord a owl:Class ;
    rdfs:label "IngestionRecord" .

dg:graphFile    a owl:DatatypeProperty ; rdfs:label "graphFile"    ; rdfs:range xsd:string .
dg:addedAt      a owl:DatatypeProperty ; rdfs:label "addedAt"      ; rdfs:range xsd:dateTime .
dg:detectedRole a owl:ObjectProperty   ; rdfs:label "detectedRole" .

# ── Software-agent metadata ───────────────────────────────────────────────────
dg:provider     a owl:DatatypeProperty ; rdfs:label "provider" ; rdfs:range xsd:string .
dg:modelId      a owl:DatatypeProperty ; rdfs:label "modelId"  ; rdfs:range xsd:string .
"""

_UNRESOLVED_TTL = """\
@prefix dg:  <http://example.org/docgraph/meta#> .
@prefix lis: <http://standards.iso.org/iso/15926/part14/> .

# Stubs for concepts referenced before their defining document was added.
# Each stub: a class typed as lis:InformationObject with dg:status dg:Unresolved
# and dg:firstSeenIn pointing to the source that first mentioned it.
"""

_SOURCES_TTL = """\
@prefix dg:      <http://example.org/docgraph/meta#> .
@prefix lis:     <http://standards.iso.org/iso/15926/part14/> .
@prefix xsd:     <http://www.w3.org/2001/XMLSchema#> .

# Registry of ingested sources. Each record is dual-typed as
# dg:IngestionRecord (admin) and lis:InformationObject (the file itself).
"""


def reset_sources(project_root: Path) -> None:
    """Overwrite sources.ttl with an empty registry (header only)."""
    sources_path(project_root).write_text(_SOURCES_TTL)


def init_project(target: Path, console: Console, *, force: bool = False) -> None:
    """Create the ``.docgraph/`` directory inside *target*.

    Raises ``FileExistsError`` if ``.docgraph/`` already exists and *force* is False.
    """
    dg_dir   = target / DOCGRAPH_DIR
    g_dir    = dg_dir / GRAPHS_SUBDIR
    c_dir    = dg_dir / CACHE_SUBDIR

    if dg_dir.exists() and not force:
        raise FileExistsError(f"{dg_dir} already exists. Use --force to reinitialise.")
    if dg_dir.exists() and force:
        shutil.rmtree(dg_dir)

    dg_dir.mkdir(parents=True)
    g_dir.mkdir()
    c_dir.mkdir()
    console.print(f"  created [dim]{dg_dir}[/dim]")

    (dg_dir / META_FILENAME).write_text(_META_TTL)
    console.print(f"  wrote   [dim]{META_FILENAME}[/dim]")

    for source, fname, label in _BUNDLED_ONTOLOGIES:
        if not source.is_file():
            raise FileNotFoundError(
                f"Bundled ontology not found at {source} ({label}). "
                "docgraph install is incomplete."
            )
        shutil.copy2(source, dg_dir / fname)
        console.print(f"  copied  [dim]{fname}[/dim] ({label})")

    (dg_dir / SOURCES_FILENAME).write_text(_SOURCES_TTL)
    console.print(f"  wrote   [dim]{SOURCES_FILENAME}[/dim]")

    (g_dir / UNRESOLVED_FILENAME).write_text(_UNRESOLVED_TTL)
    console.print(f"  wrote   [dim]{GRAPHS_SUBDIR}/{UNRESOLVED_FILENAME}[/dim]")

    console.print(
        f"\n[green]Initialised docgraph project in[/green] [bold]{target}[/bold]\n"
        f"Add a source with [dim]docgraph add <file>[/dim]."
    )
