"""ISO 15926 Part 14 extraction pipeline.

This package implements the active extraction pipeline as a decision-tree
walk over the LIS-14 upper ontology (vendor/ontologies/LIS-14.ttl). A
future Part 15 / domain-specific pipeline can slot in as a sibling
package and register its own tasks against
`src/tasks/_registry.py:docgraph`.

The orchestration lives in `src/tasks/` — each per-doc task (identity,
recognize, convert, extract, templates, align, register, diagram) is a
standalone module decorated against the project-wide registry. This
package provides the Part 14-specific helpers those tasks call into.

Layout:
  loader.py     — builds the in-memory Dataset from vendor/ontologies/ +
                  .docgraph/ (per ARCHITECTURE.md § Storage layout)
  structural.py — recognize + convert delta builders
  mega_walker.py — the mega-extraction LLM call (entities + properties +
                   ext-class proposals in one batch)
  ext_ontology.py / consolidate.py — LLM-proposed extension classes
                   plus their cross-doc consolidation (see
                   docs/architecture/rdl-scopes.md)
  template_recognizer.py — SPARQL-based template fold pass
  enrich.py     — external-RDL refinement (POSC Caesar, …)
"""
