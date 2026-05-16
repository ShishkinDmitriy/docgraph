"""Two-tier dedup of LLM-proposed ext: classes across docs.

Tier 1 — anchor-scoped cosine pre-filter (mechanical):
    For each NEW ext class, find same-anchor existing classes whose
    embedding is close enough to be plausibly related (cosine ≥
    SHORTLIST_THRESHOLD). Top-K become a shortlist.

Tier 2 — LLM relation classifier (one batched call):
    For each new class with a non-empty shortlist, the LLM picks ONE
    set-theoretic relation:

      equivalent_to:   ext:X   — same kind; substitute new with canonical
                                  AND enrich canonical with skos:altLabel
                                  + skos:scopeNote contributed by the new
                                  doc (additive, doesn't mutate canonical's
                                  declaring graph).
      subclass_of:     ext:X   — new IS-A canonical; keep new + add
                                  `<new> rdfs:subClassOf <canonical>` in
                                  this doc's graph.
      superclass_of:   ext:X   — new IS-PARENT-OF canonical; keep new +
                                  add `<canonical> rdfs:subClassOf <new>`.
      unrelated                — keep new as standalone class.

Anchor-scoped throughout: lis:InformationObject extensions never compare
against lis:Activity extensions. Better recall AND precision.

Reuses src/embeddings.py (OpenAI text-embedding-3-small, file-backed
EmbeddingStore at .docgraph/embeddings.npz). Graceful no-op if
OPENAI_API_KEY is absent — extraction continues without dedup.

Deferred to a follow-up:
  - Multi-relation per new class (LLM picks ONE for now)
  - Mint intermediate parent classes (LLM never proposes a new parent)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, SKOS

from src.embeddings import (
    EmbeddingClient,
    EmbeddingError,
    EmbeddingStore,
    cosine_topk,
)
from src.extract_part14.ext_ontology import (
    DG,
    ExtClass,
    extract_classes_from_graph,
)
from src.llm import LLMClient, TextBlock
from src.log_panels import log_prompt, log_response
from src.models import ModelConfig

logger = logging.getLogger(__name__)


# Cosine ≥ this means the LLM is asked to judge the relation. Below this,
# we don't even ask — the new class is kept as standalone.
SHORTLIST_THRESHOLD  = 0.65

# Top-K candidates per new class shown to the LLM in the relation prompt.
SHORTLIST_TOP_K      = 5


# ── Decision shape ──────────────────────────────────────────────────────


@dataclass
class RelationDecision:
    """One LLM relation judgment for a new ext class vs the project's
    existing classes."""
    new_class:      ExtClass
    relation:       str          # "equivalent_to" | "subclass_of" | "superclass_of" | "unrelated"
    target:         ExtClass | None        # the canonical class for non-"unrelated"
    similarity:     float                  # cosine to the picked target
    reason:         str                    # LLM's explanation


# ── Top-level orchestrator ──────────────────────────────────────────────


def walk_dedup(
    extract_graph:    Graph,
    templates_graph:  Graph | None,
    *,
    ontology:         Graph,
    embedding_store:  EmbeddingStore,
    embedding_client: EmbeddingClient,
    llm_client:       LLMClient | None = None,
    llm_model:        ModelConfig | None = None,
    shortlist_threshold: float = SHORTLIST_THRESHOLD,
    shortlist_top_k:     int   = SHORTLIST_TOP_K,
    console=None,
) -> list[RelationDecision]:
    """Anchor-scoped dedup phase.

    Sub-phases:
      1. Build (new, candidates[]) shortlist via cosine pre-filter.
      2. Single batched LLM call to classify the relation per new class.
      3. Apply each decision to extract_graph (and templates_graph if given).

    Mutates the graphs in place. Returns the decisions made.

    If *llm_client* is None, falls back to the previous behavior:
    auto-substitute on cosine ≥ 0.88 (LEGACY_AUTO_THRESHOLD), no
    relation classifier. Only used as a safety net for tests; the
    pipeline always passes llm_client.
    """
    new_classes_by_slug = extract_classes_from_graph(extract_graph)
    if not new_classes_by_slug:
        return []

    existing_classes_by_slug = {
        slug: cls for slug, cls in extract_classes_from_graph(ontology).items()
        if slug not in new_classes_by_slug
    }

    if not existing_classes_by_slug:
        # First doc — embed new classes for future docs to dedup against.
        _embed_and_store(list(new_classes_by_slug.values()),
                         embedding_store, embedding_client)
        return []

    # ── Tier 1: cosine pre-filter (anchor-scoped) ──
    shortlists = _build_shortlists(
        list(new_classes_by_slug.values()),
        existing_classes_by_slug,
        embedding_store, embedding_client,
        threshold=shortlist_threshold, top_k=shortlist_top_k,
    )

    # New classes with NO shortlist candidates → keep as new (no LLM call needed).
    # `_build_shortlists` already embedded+stored those, so nothing more to do.
    needs_llm = {slug: pair for slug, pair in shortlists.items() if pair[1]}

    if not needs_llm:
        return []

    # ── Tier 2: batched LLM relation classifier ──
    if llm_client is None:
        # Legacy fallback (used only by tests pre-LLM): auto-substitute
        # high-cosine pairs.
        decisions = _legacy_auto_substitute(needs_llm, embedding_store)
    else:
        decisions = _classify_relations(
            needs_llm, llm_client=llm_client, llm_model=llm_model, console=console,
        )

    # ── Apply each decision ──
    for d in decisions:
        if d.relation == "equivalent_to" and d.target is not None:
            _apply_equivalent(extract_graph,   d)
            if templates_graph is not None:
                _apply_equivalent(templates_graph, d)
        elif d.relation in ("subclass_of", "superclass_of") and d.target is not None:
            _apply_subclass(extract_graph,    d)
            if templates_graph is not None:
                _apply_subclass(templates_graph, d)
            # Keep new class's embedding (it survives as a standalone class)
            new_vec = embedding_client.embed([embed_text_for_class(d.new_class)])[0]
            embedding_store.upsert_class(str(d.new_class.uri), new_vec)
        else:
            # "unrelated" → keep new as-is, store its embedding.
            new_vec = embedding_client.embed([embed_text_for_class(d.new_class)])[0]
            embedding_store.upsert_class(str(d.new_class.uri), new_vec)
        if console:
            _log_decision(d, console)

    return decisions


# ── Tier 1: shortlist building ──────────────────────────────────────────


def _build_shortlists(
    new_classes:      list[ExtClass],
    existing_by_slug: dict[str, ExtClass],
    store:            EmbeddingStore,
    client:           EmbeddingClient,
    *,
    threshold:        float,
    top_k:            int,
) -> dict[str, tuple[ExtClass, list[tuple[ExtClass, float]]]]:
    """For each new class, return up to top_k same-anchor existing classes
    with cosine ≥ threshold. Returns dict keyed by new class slug (since
    ExtClass isn't hashable) → (new_class, [(candidate, similarity), …]).

    Side effect: backfills missing embeddings for candidate classes and
    stores embeddings of new classes whose shortlist ends up empty (so
    future docs can dedup against them).
    """
    needed_anchors = {c.anchor for c in new_classes}
    relevant_existing = [c for c in existing_by_slug.values()
                         if c.anchor in needed_anchors]

    # Backfill embeddings for relevant existing classes (one batched call).
    backfill = [c for c in relevant_existing
                if not store.has_class(str(c.uri))]
    if backfill:
        texts   = [embed_text_for_class(c) for c in backfill]
        vectors = client.embed(texts)
        for c, v in zip(backfill, vectors):
            store.upsert_class(str(c.uri), v)

    # Embed all new classes (one batched call).
    new_vectors = client.embed([embed_text_for_class(c) for c in new_classes])

    out: dict[str, tuple[ExtClass, list[tuple[ExtClass, float]]]] = {}
    uri_to_idx = {u: i for i, u in enumerate(store.class_uris)}
    for new_cls, new_vec in zip(new_classes, new_vectors):
        same_anchor = [c for c in relevant_existing
                       if c.anchor == new_cls.anchor
                       and store.has_class(str(c.uri))]
        if not same_anchor:
            store.upsert_class(str(new_cls.uri), new_vec)
            out[new_cls.slug] = (new_cls, [])
            continue

        cand_uris    = [str(c.uri) for c in same_anchor]
        cand_indices = [uri_to_idx[u] for u in cand_uris]
        cand_vectors = store.class_vectors[cand_indices]
        topk = cosine_topk(new_vec, cand_vectors, cand_uris, k=top_k)
        kept = [(uri, sim) for uri, sim in topk if sim >= threshold]
        if not kept:
            store.upsert_class(str(new_cls.uri), new_vec)
            out[new_cls.slug] = (new_cls, [])
            continue

        uri_to_cls = {str(c.uri): c for c in same_anchor}
        out[new_cls.slug] = (new_cls, [(uri_to_cls[uri], sim) for uri, sim in kept])
    return out


# ── Tier 2: LLM relation classifier ─────────────────────────────────────


_RELATION_PROMPT = """\
You are deciding the set-theoretic relation between newly proposed
extension classes and existing extension classes in a knowledge graph.

For each numbered question, pick exactly ONE relation:

  - equivalent_to:   <Cn>   — the new class IS THE SAME KIND as Cn.
                              They differ only in surface label / phrasing.
                              Choose this when collapsing them loses no
                              meaningful distinction.
  - subclass_of:     <Cn>   — the new class is a MORE SPECIFIC kind of Cn
                              (every instance of new is also an instance of
                              Cn, but Cn has instances that aren't new).
                              E.g. DentalTreatment subclass_of Treatment.
  - superclass_of:   <Cn>   — the new class is a MORE GENERAL kind that
                              CONTAINS Cn. E.g. proposing a new "Treatment"
                              when the project already has "DentalTreatment".
  - unrelated               — no useful relation; keep new as standalone.

Be conservative: prefer "unrelated" over a wrong relation; prefer
"subclass_of" over "equivalent_to" when one is genuinely more specific.

== Questions ==

{questions_block}

Reply with JSON only, one entry per question id:

{{
  "Q1": {{"relation": "equivalent_to" | "subclass_of" | "superclass_of" | "unrelated",
          "target":   "<Cn label OR empty for 'unrelated'>",
          "reason":   "<one short sentence explaining the choice>"}},
  "Q2": {{...}},
  ...
}}
"""


def _classify_relations(
    needs_llm: dict[str, tuple[ExtClass, list[tuple[ExtClass, float]]]],
    *,
    llm_client: LLMClient,
    llm_model:  ModelConfig | None,
    console=None,
) -> list[RelationDecision]:
    """One batched LLM call — returns one RelationDecision per new class.

    Maps responses back to ExtClass instances and validates the target
    label resolves to one of the candidates we offered. Unrecognized
    targets (LLM hallucinations) downgrade to 'unrelated'.
    """
    items = [(new_cls, cands) for new_cls, cands in needs_llm.values()]
    questions_block = []
    for i, (new_cls, cands) in enumerate(items, 1):
        qid = f"Q{i}"
        block = [
            f"{qid}. NEW class: ext:{new_cls.slug}",
            f"    label:   \"{new_cls.label}\"",
        ]
        if new_cls.alt_labels:
            block.append(f"    alts:    {', '.join(new_cls.alt_labels)!r}")
        if new_cls.comment:
            block.append(f"    comment: {new_cls.comment!r}")
        block.append(f"    anchor:  {_curie(new_cls.anchor)}")
        block.append("")
        block.append("    Candidates (same anchor scope, ranked by cosine similarity):")
        for j, (cand, sim) in enumerate(cands, 1):
            cand_id = f"C{j}"
            line = (f"      {cand_id}. ext:{cand.slug}  (sim {sim:.2f}) — "
                    f"{cand.label!r}")
            if cand.comment:
                line += f" — {cand.comment[:120]!r}"
            block.append(line)
        questions_block.append("\n".join(block))

    prompt = _RELATION_PROMPT.format(questions_block="\n\n".join(questions_block))
    if console:
        console.print(f"  classifying {len(items)} new class(es) "
                      f"vs same-anchor existing — one batched LLM call...")
    model_id = llm_model.model_id if llm_model else "claude-haiku-4-5"
    meta = f"{model_id}  ext-class relations"
    log_prompt("part14/ext-dedup", prompt, logger=logger, metadata=meta)
    resp = llm_client.create(
        model_id=model_id,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2048,
    )
    text = "".join(b.text for b in resp.content if isinstance(b, TextBlock)).strip()
    log_response("part14/ext-dedup", text, logger=logger, metadata=meta, as_json=True)
    parsed = _parse_relation_response(text, qids=[f"Q{i}" for i in range(1, len(items) + 1)])

    decisions: list[RelationDecision] = []
    for i, (new_cls, cands) in enumerate(items, 1):
        qid = f"Q{i}"
        entry = parsed.get(qid) or {}
        relation_str = entry.get("relation", "unrelated").strip()
        target_str   = entry.get("target",   "").strip()
        reason       = entry.get("reason",   "").strip()

        if relation_str not in ("equivalent_to", "subclass_of", "superclass_of", "unrelated"):
            relation_str = "unrelated"

        target_cls: ExtClass | None = None
        sim = 0.0
        if relation_str != "unrelated" and target_str:
            # Match LLM's `target` against the candidates' slug or label
            for cand, csim in cands:
                if (target_str == f"ext:{cand.slug}"
                        or target_str == cand.slug
                        or target_str == cand.label
                        or target_str.casefold() == cand.label.casefold()):
                    target_cls, sim = cand, csim
                    break
            if target_cls is None:
                # LLM named something not on the shortlist — downgrade.
                logger.info("ext_dedup: %s LLM target %r not in shortlist; downgrading to unrelated",
                            new_cls.slug, target_str)
                relation_str = "unrelated"

        decisions.append(RelationDecision(
            new_class=new_cls, relation=relation_str,
            target=target_cls, similarity=sim, reason=reason,
        ))
    return decisions


def _parse_relation_response(text: str, *, qids: list[str]) -> dict[str, dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if cleaned.count("```") >= 2 else cleaned
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        payload = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError:
        return {}
    out: dict[str, dict] = {}
    for qid in qids:
        v = payload.get(qid)
        if isinstance(v, dict):
            out[qid] = {
                "relation": str(v.get("relation", "") or "").strip(),
                "target":   str(v.get("target",   "") or "").strip(),
                "reason":   str(v.get("reason",   "") or "").strip(),
            }
    return out


# ── Apply: per-relation graph mutations ─────────────────────────────────


def _apply_equivalent(graph: Graph, d: RelationDecision) -> None:
    """Substitute new class with canonical, AND enrich canonical with the
    new class's label/altLabels/comment as skos: contributions in this
    doc's graph (additive — doesn't mutate the canonical's declaring graph).
    """
    new_cls       = d.new_class
    canonical_uri = d.target.uri    # type: ignore[union-attr]

    # 1. Drop new class definition (canonical's def lives elsewhere).
    for s, p, o in list(graph.triples((new_cls.uri, None, None))):
        graph.remove((s, p, o))

    # 2. Rewrite ALL triples with new class URI as object → canonical.
    instances_substituted: set = set()
    for s, p, o in list(graph.triples((None, None, new_cls.uri))):
        graph.remove((s, p, o))
        graph.add((s, p, canonical_uri))
        if p == RDF.type:
            instances_substituted.add(s)

    # 3. Audit per-instance.
    for inst in instances_substituted:
        graph.add((inst, DG.proposedAs, Literal(new_cls.slug)))

    # 4. Enrich canonical with the new class's slug + label + altLabels
    #    (all become skos:altLabel triples on the canonical, in THIS doc's
    #    graph — additive, doesn't mutate the canonical's declaring graph).
    #    The slug is included because it was the LLM's first-class name
    #    for this kind and is likely a useful synonym for downstream
    #    reuse / search. Comment lands as skos:scopeNote.
    enrich_alts = [new_cls.slug, new_cls.label] + list(new_cls.alt_labels)
    existing_alts = {str(o) for o in graph.objects(canonical_uri, SKOS.altLabel)}
    canonical_label = d.target.label                                # type: ignore[union-attr]
    for alt in enrich_alts:
        if not alt or alt in existing_alts or alt == canonical_label:
            continue
        graph.add((canonical_uri, SKOS.altLabel, Literal(alt)))
        existing_alts.add(alt)
    if new_cls.comment and new_cls.comment != getattr(d.target, "comment", ""):
        graph.add((canonical_uri, SKOS.scopeNote, Literal(new_cls.comment)))


def _apply_subclass(graph: Graph, d: RelationDecision) -> None:
    """Add a `rdfs:subClassOf` link reflecting the LLM's relation, in
    THIS doc's graph (the doc that introduced both the new class and the
    relation assertion). New class definition stays — unlike equivalent,
    it survives as a standalone class with its own URI.
    """
    if d.target is None:
        return
    if d.relation == "subclass_of":
        graph.add((d.new_class.uri, RDFS.subClassOf, d.target.uri))
    elif d.relation == "superclass_of":
        graph.add((d.target.uri, RDFS.subClassOf, d.new_class.uri))


# ── Embedding text helpers ──────────────────────────────────────────────


def embed_text_for_class(cls: ExtClass) -> str:
    """Build the embedding text for an ExtClass: label + alt labels + comment."""
    parts: list[str] = [cls.label]
    if cls.alt_labels:
        parts.append(", ".join(cls.alt_labels))
    if cls.comment:
        parts.append(cls.comment)
    return "\n".join(parts)


def _embed_and_store(
    classes: list[ExtClass],
    store:   EmbeddingStore,
    client:  EmbeddingClient,
) -> None:
    if not classes:
        return
    texts   = [embed_text_for_class(c) for c in classes]
    vectors = client.embed(texts)
    for c, v in zip(classes, vectors):
        store.upsert_class(str(c.uri), v)


# ── Legacy fallback (used only when llm_client is None — tests) ─────────


@dataclass
class Substitution:
    """Back-compat shim — emitted only when walk_dedup runs in legacy
    mode (no LLM client provided). Pure cosine substitute. Real pipeline
    runs use RelationDecision."""
    proposed_uri:   URIRef
    proposed_slug:  str
    canonical_uri:  URIRef
    canonical_slug: str
    similarity:     float


_LEGACY_AUTO_THRESHOLD = 0.88


def _legacy_auto_substitute(
    needs_llm: dict[str, tuple[ExtClass, list[tuple[ExtClass, float]]]],
    store: EmbeddingStore,
) -> list[RelationDecision]:
    """Backwards-compatible behavior used by tests that don't supply an
    LLM client: auto-substitute when top-1 cosine ≥ 0.88, else keep new.
    """
    out: list[RelationDecision] = []
    for new_cls, cands in needs_llm.values():
        if not cands:
            continue
        target_cls, sim = cands[0]
        if sim >= _LEGACY_AUTO_THRESHOLD:
            out.append(RelationDecision(
                new_class=new_cls, relation="equivalent_to",
                target=target_cls, similarity=sim,
                reason=f"legacy auto-substitute (cosine {sim:.2f})",
            ))
    return out


def apply_substitution(graph: Graph, sub: Substitution) -> None:
    """Back-compat shim: mirror the original pure-substitute behavior so
    older tests keep passing. The new equivalent-path enriches via
    skos:altLabel + skos:scopeNote — those tests should migrate to
    _apply_equivalent."""
    for s, p, o in list(graph.triples((sub.proposed_uri, None, None))):
        graph.remove((s, p, o))
    instances_substituted: set = set()
    for s, p, o in list(graph.triples((None, None, sub.proposed_uri))):
        graph.remove((s, p, o))
        graph.add((s, p, sub.canonical_uri))
        if p == RDF.type:
            instances_substituted.add(s)
    for inst in instances_substituted:
        graph.add((inst, DG.proposedAs, Literal(sub.proposed_slug)))


# ── Console helpers ─────────────────────────────────────────────────────


def _log_decision(d: RelationDecision, console) -> None:
    """One-line console summary per decision."""
    if d.relation == "unrelated":
        kind = "kept ext:" + d.new_class.slug
        suffix = " (no related candidate)" if not d.reason else f" — {d.reason}"
        console.print(f"    [dim]{kind}{suffix}[/dim]")
        return
    target_slug = f"ext:{d.target.slug}" if d.target else "?"
    sym = {
        "equivalent_to": "≡",
        "subclass_of":   "⊆",
        "superclass_of": "⊇",
    }[d.relation]
    msg = f"    [dim]ext:{d.new_class.slug} {sym} {target_slug} (sim {d.similarity:.2f})"
    if d.reason:
        msg += f" — {d.reason}"
    msg += "[/dim]"
    console.print(msg)


# ── Internal CURIE helper (to avoid coupling to the existing one in
# root_walker — keeps dedup module standalone). ────────────────────────


_LIS_NS = "http://rds.posccaesar.org/ontology/lis14/rdl/"


def _curie(uri) -> str:
    s = str(uri)
    if s.startswith(_LIS_NS):
        return f"lis:{s[len(_LIS_NS):]}"
    return f"<{s}>"
