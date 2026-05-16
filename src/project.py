"""Project root discovery and .docgraph directory initialisation.

Layout (per ARCHITECTURE.md):

    .docgraph/
      meta.ttl                         — docgraph extensions, owl:imports the upper ontologies
      iso-15926-2.rdf                  — ISO 15926 Part 2 OWL upper ontology (POSC Caesar)
      iso-15926-2-annotations.rdf      — Part 2 entity definitions / notes / examples
      prov-o.ttl                       — W3C PROV-O (provenance)
      dcterms.ttl                      — DCMI Terms (bibliographic metadata)
      sources.ttl                      — registry of ingested sources
      graphs/
        <slug>.ttl                     — HEAD snapshots per source (real file)
        doc-<slug>.NNN.trig            — versioned-graph deltas per source
      cache/                           — PDF→Markdown cache (unchanged)
"""

import shutil
from pathlib import Path

from rich.console import Console

DOCGRAPH_DIR                     = ".docgraph"
META_FILENAME                    = "meta.ttl"            # part2 pipeline only
CONFIG_FILENAME                  = "config.ttl"          # part14 pipeline
ISO15926_FILENAME                = "iso-15926-2.rdf"
ISO15926_ANNOTATIONS_FILENAME    = "iso-15926-2-annotations.rdf"
PROV_O_FILENAME                  = "prov-o.ttl"
DCTERMS_FILENAME                 = "dcterms.ttl"
SOURCES_FILENAME                 = "sources.ttl"
CACHE_SUBDIR                     = "cache"
ONTOLOGIES_SUBDIR                = "ontologies"
EXT_FILENAME                     = "ext.ttl"

# Legacy (Part 2 + dormant Part 14 enrich) — flat `graphs/` directory
# that holds per-source .ttl files. New Part 14 code uses the per-doc
# layout below instead.
GRAPHS_SUBDIR                    = "graphs"

# Per-scope grouping (Part 14, current layout).
DOCS_SUBDIR                      = "docs"        # docs/<slug>/...
PROJECT_SCOPE_SUBDIR             = "project"     # project/...
RDL_SCOPE_SUBDIR                 = "rdl"         # rdl/<id>/...

# Filenames inside a scope's dir.
CANONICAL_HTML_FILENAME          = "canonical.html"   # the LLM-rendered HTML
PROMPT_MD_FILENAME               = "prompt.md"        # the markdown view fed to LLM
ANNOTATED_HTML_FILENAME          = "annotated.html"   # derived viewer artifact

# Pipelines (see ARCHITECTURE.md § Pipelines — Part 2 and Part 14 in parallel)
PIPELINE_PART2  = "part2"
PIPELINE_PART14 = "part14"
PIPELINES       = (PIPELINE_PART2, PIPELINE_PART14)
DEFAULT_PIPELINE = PIPELINE_PART14  # Part 14 is the active pipeline; Part 2 kept for legacy

# Bundled upper-ontology sources.
_VENDOR_ONTOLOGIES_DIR          = Path(__file__).parent.parent / "vendor" / "ontologies"
_ISO15926_SOURCE                = _VENDOR_ONTOLOGIES_DIR / "ISO-15926-2_2003.rdf"
_ISO15926_ANNOTATIONS_SOURCE    = _VENDOR_ONTOLOGIES_DIR / "ISO-15926-2_2003_annotations.rdf"
_PROV_O_SOURCE                  = _VENDOR_ONTOLOGIES_DIR / "prov-o.ttl"
_DCTERMS_SOURCE                 = _VENDOR_ONTOLOGIES_DIR / "dcterms.ttl"

_BUNDLED_ONTOLOGIES = [
    (_ISO15926_SOURCE,             ISO15926_FILENAME,             "ISO 15926 Part 2"),
    (_ISO15926_ANNOTATIONS_SOURCE, ISO15926_ANNOTATIONS_FILENAME, "ISO 15926 Part 2 (annotations)"),
    (_PROV_O_SOURCE,               PROV_O_FILENAME,               "W3C PROV-O"),
    (_DCTERMS_SOURCE,              DCTERMS_FILENAME,              "DCMI Terms"),
]

# Files that may be left over from the pre-Part-2 layout. Removed during migration.


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


def config_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / CONFIG_FILENAME


def iso15926_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / ISO15926_FILENAME


def iso15926_annotations_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / ISO15926_ANNOTATIONS_FILENAME


def prov_o_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / PROV_O_FILENAME


def dcterms_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / DCTERMS_FILENAME


def sources_path(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / SOURCES_FILENAME


def cache_dir(project_root: Path) -> Path:
    return project_root / DOCGRAPH_DIR / CACHE_SUBDIR


def graphs_dir(project_root: Path) -> Path:
    """Legacy `.docgraph/graphs/` — used by the Part 2 pipeline and the
    (dormant) Part 14 enrich step that still writes flat .ttl files.
    New Part 14 code uses `doc_dir(slug)` per the per-scope layout."""
    return project_root / DOCGRAPH_DIR / GRAPHS_SUBDIR


# ── Per-scope dirs (one dir per scope kind + name) ─────────────────────────


def doc_dir(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/` — every artifact for one doc lives here:
    delta.NNN.trig files, canonical.html (LLM-rendered), prompt.md
    (LLM-prompt view), annotated.html (derived viewer), snapshot.*.ttl
    (on demand). Easy to `rm -rf` a single doc."""
    return project_root / DOCGRAPH_DIR / DOCS_SUBDIR / slug


def project_scope_dir(project_root: Path) -> Path:
    """`.docgraph/project/` — project-scope deltas (e.g. promoted ext
    classes), parallel structure to doc dirs."""
    return project_root / DOCGRAPH_DIR / PROJECT_SCOPE_SUBDIR


def rdl_scope_dir(project_root: Path, rdl_id: str) -> Path:
    """`.docgraph/rdl/<id>/` — cached remote RDL data as deltas."""
    return project_root / DOCGRAPH_DIR / RDL_SCOPE_SUBDIR / rdl_id


# ── Per-doc artifact paths (the typed files inside a doc_dir) ──────────────


def canonical_html_path(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/canonical.html` — LLM-rendered HTML.
    Source of truth for structure + atomic-unit IDs."""
    return doc_dir(project_root, slug) / CANONICAL_HTML_FILENAME


def prompt_md_path(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/prompt.md` — markdown projection of the
    canonical HTML that's fed to the extract LLM as the prompt input.
    Cached on disk so the LLM call is reproducible + inspectable."""
    return doc_dir(project_root, slug) / PROMPT_MD_FILENAME


def annotated_html_path(project_root: Path, slug: str) -> Path:
    """`.docgraph/docs/<slug>/annotated.html` — derived viewer artifact
    from `docgraph view <slug>`. Regenerable any time."""
    return doc_dir(project_root, slug) / ANNOTATED_HTML_FILENAME


def ontologies_dir(project_root: Path) -> Path:
    """Per-project mutable ontologies directory.

    Currently holds `ext.ttl` — LLM-proposed extension classes anchored
    under stable LIS-14 superclasses. Distinct from the immutable bundled
    foundationals in `vendor/ontologies/`. Loaded by the part14 loader
    into its own named graph so it's visible to extraction and enrich."""
    return project_root / DOCGRAPH_DIR / ONTOLOGIES_SUBDIR


def ext_ontology_path(project_root: Path) -> Path:
    """Path to the per-project extension ontology (LLM-proposed classes)."""
    return ontologies_dir(project_root) / EXT_FILENAME


def embeddings_path(project_root: Path) -> Path:
    """Path to the project-wide embedding store (`.docgraph/embeddings.npz`).

    Used by the ext-class dedup phase to detect near-duplicate proposed
    classes across docs (e.g. ext:Bill collapsing into an existing
    ext:Invoice when their label/comment embeddings are close).
    """
    return project_root / DOCGRAPH_DIR / "embeddings.npz"


# NOTE: The dg: vocabulary below is duplicated in vendor/ontologies/dg.ttl, which
# is the canonical source-of-truth going forward (per ARCHITECTURE.md storage
# layout). This inline template is kept until the loader refactor (M0/M1 of the
# parallel-pipelines plan) replaces .docgraph/meta.ttl with .docgraph/config.ttl
# and reads dg.ttl from vendor/. Keep the two in sync until then.
_META_TTL = """\
@prefix dg:       <urn:docgraph:vocab:meta#> .
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix prov:     <http://www.w3.org/ns/prov#> .
@prefix dcterms:  <http://purl.org/dc/terms/> .
@prefix owl:      <http://www.w3.org/2002/07/owl#> .
@prefix rdf:      <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs:     <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd:      <http://www.w3.org/2001/XMLSchema#> .

<urn:docgraph:vocab:meta>  a owl:Ontology ;
    rdfs:label   "DocGraph meta-ontology" ;
    rdfs:comment "Extensions on top of ISO 15926 Part 2, PROV-O, and DCMI Terms." ;
    owl:imports  <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003> ,
                 <http://www.w3.org/ns/prov-o-20130430> ,
                 <http://purl.org/dc/terms/> .

# ── Light alignment between Part 2 and PROV-O ─────────────────────────────────
# (prov:Agent is intentionally not aligned — it spans Person, Organization, SoftwareAgent.)
prov:Activity  rdfs:subClassOf iso15926:Activity .
prov:Entity    rdfs:subClassOf iso15926:Thing .

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

# ── Document subject — what a source document is about ───────────────────────
# Part 2 has no instance-level InformationObject; sources are typed as
# iso15926:WholeLifeIndividual + an ad-hoc subclass of ClassOfInformationObject.
dg:isAbout    a owl:ObjectProperty ;
    rdfs:label  "isAbout" ;
    rdfs:domain iso15926:WholeLifeIndividual ;
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

# ── Extraction confidence + reason (general — attached to a prov:Activity) ───
dg:confidence   a owl:DatatypeProperty ; rdfs:label "confidence" ; rdfs:range xsd:decimal .
dg:reason       a owl:DatatypeProperty ; rdfs:label "reason"     ; rdfs:range xsd:string  .

# ── Type / form-classification signals (attached to the extraction graph) ────
dg:typeConfidence        a owl:DatatypeProperty ; rdfs:label "typeConfidence"        ; rdfs:range xsd:decimal .
dg:typeCoverage          a owl:DatatypeProperty ; rdfs:label "typeCoverage"          ; rdfs:range xsd:decimal .
dg:typeNearestSimilarity a owl:DatatypeProperty ; rdfs:label "typeNearestSimilarity" ; rdfs:range xsd:decimal .

# ── Document outside the ontology's coverage (form scope below threshold) ────
dg:UncoveredDocument a owl:NamedIndividual ; rdfs:label "UncoveredDocument" .
"""

_SOURCES_TTL = """\
@prefix dg:       <urn:docgraph:vocab:meta#> .
@prefix iso15926: <http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#> .
@prefix xsd:      <http://www.w3.org/2001/XMLSchema#> .

# Registry of ingested sources. Each record is dual-typed as
# dg:IngestionRecord (admin) and iso15926:WholeLifeIndividual (the file itself).
"""

# Minimal per-project header for the part14 pipeline. No copies of foundational
# ontologies — the loader reads them from vendor/ontologies/ at startup based on
# the dg:pipeline value below. See ARCHITECTURE.md § Storage layout.
_CONFIG_TTL_PART14 = """\
@prefix dg:  <urn:docgraph:vocab:meta#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

<> a dg:DocgraphProject ;
    dg:pipeline   dg:Part14Pipeline ;
    dg:createdAt  "{created_at}"^^xsd:date ;
    dg:version    "0.1.0" .
"""

_TEMPLATES_REGISTRY_TTL = """\
@prefix dg:  <urn:docgraph:vocab:meta#> .

# Registry of user-authored templates loaded by this project.
# Each entry: a dg:TemplateRegistration with dg:templatePath pointing at a TTL
# file in the project repo (typically under data/templates/<custom>/).
# Bundled templates (data/templates/iso14/, data/templates/bridges/) and the
# core tpl: vocabulary are not registered here — the loader picks them up
# automatically based on the project's pipeline.
"""


def reset_sources(project_root: Path) -> None:
    """Overwrite sources.ttl with an empty registry (header only)."""
    sources_path(project_root).write_text(_SOURCES_TTL)


def init_project(
    target: Path,
    console: Console,
    *,
    force: bool = False,
    pipeline: str = DEFAULT_PIPELINE,
) -> None:
    """Create the ``.docgraph/`` directory inside *target*.

    *pipeline* picks the upper-ontology pipeline:
      "part2"  — legacy: writes meta.ttl, copies foundationals into .docgraph/
      "part14" — modern: writes config.ttl only; loader reads foundationals
                 from vendor/ontologies/ at startup

    Raises ``FileExistsError`` if ``.docgraph/`` already exists and *force* is False.
    Raises ``ValueError`` for an unknown pipeline.
    """
    if pipeline not in PIPELINES:
        raise ValueError(f"unknown pipeline {pipeline!r}; expected one of {PIPELINES}")

    dg_dir   = target / DOCGRAPH_DIR
    docs_dir = dg_dir / DOCS_SUBDIR
    c_dir    = dg_dir / CACHE_SUBDIR

    if dg_dir.exists() and not force:
        raise FileExistsError(f"{dg_dir} already exists. Use --force to reinitialise.")
    if dg_dir.exists() and force:
        shutil.rmtree(dg_dir)

    dg_dir.mkdir(parents=True)
    docs_dir.mkdir()
    c_dir.mkdir()
    console.print(f"  created [dim]{dg_dir}[/dim] (pipeline: [bold]{pipeline}[/bold])")

    if pipeline == PIPELINE_PART2:
        # Legacy layout: per-project copies of foundationals + verbose meta.ttl.
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
    else:
        # part14: tiny config.ttl + empty templates registry. Loader reads
        # foundationals from vendor/ontologies/ at startup.
        from datetime import date
        (dg_dir / CONFIG_FILENAME).write_text(
            _CONFIG_TTL_PART14.format(created_at=date.today().isoformat())
        )
        console.print(f"  wrote   [dim]{CONFIG_FILENAME}[/dim]")
        (dg_dir / "templates.ttl").write_text(_TEMPLATES_REGISTRY_TTL)
        console.print(f"  wrote   [dim]templates.ttl[/dim]")

    (dg_dir / SOURCES_FILENAME).write_text(_SOURCES_TTL)
    console.print(f"  wrote   [dim]{SOURCES_FILENAME}[/dim]")

    console.print(
        f"\n[green]Initialised docgraph project in[/green] [bold]{target}[/bold]\n"
        f"Add a source with [dim]docgraph add <file>[/dim]."
    )


def read_pipeline(project_root: Path) -> str:
    """Return the project's pipeline ("part2" or "part14").

    For projects with config.ttl (part14 layout), reads dg:pipeline directly.
    For projects with meta.ttl (legacy part2 layout), returns "part2".
    Raises FileNotFoundError if neither file exists.
    """
    cfg = config_path(project_root)
    if cfg.is_file():
        text = cfg.read_text(encoding="utf-8")
        if "Part14Pipeline" in text:
            return PIPELINE_PART14
        if "Part2Pipeline" in text:
            return PIPELINE_PART2
        # config.ttl present but no recognized pipeline triple — default to part14
        # since config.ttl is the part14-era artifact.
        return PIPELINE_PART14
    if meta_path(project_root).is_file():
        return PIPELINE_PART2
    raise FileNotFoundError(
        f"Neither {CONFIG_FILENAME} nor {META_FILENAME} found in "
        f"{project_root / DOCGRAPH_DIR}; project is not properly initialised."
    )
