"""ISO 15926 Part 14 extraction pipeline.

This package implements the active extraction pipeline as a decision-tree
walk over the LIS-14 upper ontology (vendor/ontologies/LIS-14.ttl).
It's currently the only pipeline in `src/project.py:PIPELINES`; the
dispatcher in `main.py:_ingest_pdf_dispatched` keeps a slot open for a
future upper-ontology choice.

Layout:
  pipeline.py   — top-level extract_pdf_part14() entry point
  loader.py     — builds the in-memory Dataset from vendor/ontologies/ +
                  .docgraph/ (per ARCHITECTURE.md § Storage layout)
  structural.py — recognize + convert delta builders
  mega_walker.py — the mega-extraction LLM call (entities + properties +
                   ext-class proposals in one batch)
  ext_ontology.py / ext_dedup.py / consolidate.py — LLM-proposed extension
                   classes plus their cross-doc dedup + consolidation
  template_recognizer.py — SPARQL-based template fold pass
  enrich.py     — external-RDL refinement (POSC Caesar, …)
"""
