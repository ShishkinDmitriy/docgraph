"""Embedding store + similarity search for class candidate selection.

`.docgraph/embeddings.npz` holds two vector tables:

    class_uris   (str array) → class_vectors (float32 N×D)
    doc_uris     (str array) → doc_vectors   (float32 M×D)

OpenAI ``text-embedding-3-small`` (1536 dim, ~$0.02/M tokens). On every
ingest we embed the document, score against every class, and pass the top-k
to the existing classify prompt — keeping LLM input tight regardless of how
deep/wide the class hierarchy gets.

Re-classification later will reuse the same store but pass *restrict_to*
to ``cosine_topk`` to narrow the search to descendants + siblings of the
previous class.
"""

import os
from pathlib import Path
from typing import Iterable

import numpy as np
from openai import OpenAI
from rdflib import Dataset, URIRef
from rdflib.namespace import RDF, RDFS

EMBEDDINGS_FILENAME = "embeddings.npz"
DEFAULT_MODEL       = "text-embedding-3-small"
DEFAULT_DIM         = 1536
DEFAULT_TOP_K       = 8

SKOS_DEF  = URIRef("http://www.w3.org/2004/02/skos/core#definition")
SKOS_NOTE = URIRef("http://www.w3.org/2004/02/skos/core#note")


class EmbeddingError(Exception):
    pass


# ─── OpenAI client wrapper ────────────────────────────────────────────────────

class EmbeddingClient:
    """Thin wrapper around ``openai.embeddings.create`` — batch-friendly."""

    def __init__(self, *, model: str = DEFAULT_MODEL, api_key: str | None = None):
        api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise EmbeddingError("OPENAI_API_KEY environment variable not set.")
        self._client = OpenAI(api_key=api_key)
        self.model = model

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return an N×D float32 array of embeddings."""
        if not texts:
            return np.zeros((0, DEFAULT_DIM), dtype=np.float32)
        resp = self._client.embeddings.create(model=self.model, input=texts)
        return np.asarray([d.embedding for d in resp.data], dtype=np.float32)


# ─── File-backed store ────────────────────────────────────────────────────────

class EmbeddingStore:
    """File-backed (.npz) embedding store for classes and documents.

    URIs are stored as Python strings (rdflib URIRef → str at the boundary).
    Class entries are upserted by URI; doc entries the same. ``save()`` rewrites
    the .npz atomically — small enough that we don't worry about incremental I/O.
    """

    def __init__(self, path: Path):
        self.path          = path
        self.class_uris:    list[str]   = []
        self.class_vectors: np.ndarray  = np.zeros((0, DEFAULT_DIM), dtype=np.float32)
        self.doc_uris:      list[str]   = []
        self.doc_vectors:   np.ndarray  = np.zeros((0, DEFAULT_DIM), dtype=np.float32)

    # ── persistence ────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "EmbeddingStore":
        store = cls(path)
        if path.is_file():
            data = np.load(path, allow_pickle=True)
            store.class_uris    = list(data["class_uris"])
            store.class_vectors = np.asarray(data["class_vectors"], dtype=np.float32)
            store.doc_uris      = list(data["doc_uris"])
            store.doc_vectors   = np.asarray(data["doc_vectors"], dtype=np.float32)
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            self.path,
            class_uris    = np.asarray(self.class_uris,    dtype=object),
            class_vectors = self.class_vectors,
            doc_uris      = np.asarray(self.doc_uris,      dtype=object),
            doc_vectors   = self.doc_vectors,
        )

    # ── upsert / remove ───────────────────────────────────────────────────

    def upsert_class(self, uri: str, vector: np.ndarray) -> None:
        self._upsert(self.class_uris, "class_vectors", uri, vector)

    def upsert_classes(self, uris: list[str], vectors: np.ndarray) -> None:
        for uri, vec in zip(uris, vectors):
            self.upsert_class(uri, vec)

    def upsert_doc(self, uri: str, vector: np.ndarray) -> None:
        self._upsert(self.doc_uris, "doc_vectors", uri, vector)

    def remove_class(self, uri: str) -> None:
        self._remove(self.class_uris, "class_vectors", uri)

    def remove_doc(self, uri: str) -> None:
        self._remove(self.doc_uris, "doc_vectors", uri)

    def has_class(self, uri: str) -> bool:
        return uri in self.class_uris

    def has_doc(self, uri: str) -> bool:
        return uri in self.doc_uris

    def _upsert(self, uri_list: list[str], vec_attr: str, uri: str, vec: np.ndarray) -> None:
        vec = vec.astype(np.float32, copy=False)
        if vec.ndim != 1:
            raise ValueError(f"vector must be 1-D, got shape {vec.shape}")
        cur = getattr(self, vec_attr)
        if uri in uri_list:
            idx = uri_list.index(uri)
            cur[idx] = vec
        else:
            uri_list.append(uri)
            new = vec[None, :]
            setattr(self, vec_attr, np.vstack([cur, new]) if cur.size else new.copy())

    def _remove(self, uri_list: list[str], vec_attr: str, uri: str) -> None:
        if uri not in uri_list:
            return
        idx = uri_list.index(uri)
        uri_list.pop(idx)
        cur = getattr(self, vec_attr)
        setattr(self, vec_attr, np.delete(cur, idx, axis=0))


# ─── Cosine top-k ─────────────────────────────────────────────────────────────

def cosine_topk(
    query: np.ndarray,
    candidates: np.ndarray,
    candidate_uris: list[str],
    *,
    k: int = DEFAULT_TOP_K,
    restrict_to: set[str] | None = None,
) -> list[tuple[str, float]]:
    """Return the top-*k* (uri, similarity) pairs by cosine similarity.

    *restrict_to* limits the candidate set to a subset of URIs — used by
    re-classification to focus on descendants + siblings of the prior class.
    """
    if candidates.size == 0:
        return []

    if restrict_to is not None:
        keep = np.array([u in restrict_to for u in candidate_uris])
        if not keep.any():
            return []
        candidates     = candidates[keep]
        candidate_uris = [u for u, k_ in zip(candidate_uris, keep) if k_]

    q = query / (np.linalg.norm(query) + 1e-12)
    c = candidates / (np.linalg.norm(candidates, axis=1, keepdims=True) + 1e-12)
    sims = c @ q

    k = min(k, len(candidate_uris))
    if k <= 0:
        return []
    top = np.argpartition(sims, -k)[-k:]
    top = top[np.argsort(-sims[top])]
    return [(candidate_uris[i], float(sims[i])) for i in top]


# ─── Text representations ────────────────────────────────────────────────────

def class_text(ds: Dataset, class_uri: URIRef, *, max_chars: int = 1500) -> str:
    """Build the text we embed for a class — qname + label + comment +
    skos:definition + skos:note + parent labels."""
    parts: list[str] = []
    nm = ds.namespace_manager
    try:
        prefix, _, local = nm.compute_qname(class_uri, generate=False)
        parts.append(f"{prefix}:{local}" if prefix else local)
    except Exception:
        parts.append(str(class_uri))

    if (label := ds.value(class_uri, RDFS.label)) is not None:
        parts.append(f"label: {label}")
    if (comment := ds.value(class_uri, RDFS.comment)) is not None:
        parts.append(f"description: {comment}")
    if (skos_def := ds.value(class_uri, SKOS_DEF)) is not None:
        parts.append(f"definition: {skos_def}")
    if (skos_note := ds.value(class_uri, SKOS_NOTE)) is not None:
        parts.append(f"note: {skos_note}")

    for parent in ds.objects(class_uri, RDFS.subClassOf):
        if isinstance(parent, URIRef):
            if (plabel := ds.value(parent, RDFS.label)) is not None:
                parts.append(f"subtype of: {plabel}")

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars]
    return text


def document_text(markdown: str, *, max_chars: int = 2000) -> str:
    """Truncate markdown to the first *max_chars* characters for embedding."""
    return markdown[:max_chars]


# ─── Helpers for slice (b) ────────────────────────────────────────────────────

def all_classes_for_indexing(ds: Dataset) -> list[URIRef]:
    """All ``owl:Class`` declarations in the combined dataset.

    We index every owl:Class so re-classification has the full set available
    even if some classes aren't subclasses of lis:InformationObject yet.
    """
    OWL_CLASS = URIRef("http://www.w3.org/2002/07/owl#Class")
    seen: set[URIRef] = set()
    for s in ds.subjects(RDF.type, OWL_CLASS):
        if isinstance(s, URIRef):
            seen.add(s)
    return sorted(seen)


def ensure_class_embeddings(
    store: EmbeddingStore,
    ds: Dataset,
    classes: Iterable[URIRef],
    client: EmbeddingClient,
) -> int:
    """Embed any class in *classes* not yet in *store*. Returns count added."""
    missing: list[URIRef] = [c for c in classes if not store.has_class(str(c))]
    if not missing:
        return 0
    texts = [class_text(ds, c) for c in missing]
    vectors = client.embed(texts)
    store.upsert_classes([str(c) for c in missing], vectors)
    return len(missing)
