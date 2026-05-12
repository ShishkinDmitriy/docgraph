"""ISO 15926 Part 14 extraction pipeline (in development).

This package implements the part14 pipeline as a decision-tree walk over the
Part 14 upper ontology (LIS-14.ttl). Built in parallel with classify_part2/
which it will eventually replace once it reaches feature parity (M3 in the
parallel-pipelines plan; see ARCHITECTURE.md § Pipelines).

Status: skeleton. M1 (structural-only ingest) not yet wired up. Calling
``extract_pdf_part14`` raises NotImplementedError with a pointer to the plan.

Layout:
  pipeline.py   — top-level extract_pdf_part14() entry point (dispatched
                  to from main.py when the project's pipeline is part14)
  loader.py     — builds the in-memory Dataset from vendor/ontologies/ +
                  .docgraph/ (per ARCHITECTURE.md § Storage layout / Loader recipe)
  branches/     — per-branch policy + prompt files (one per Part 14 top-level
                  class) — added in M2
  walker.py     — the decision-tree walker over the loaded upper ontology
                  (added in M2)
"""
