"""Part 2 reification helpers.

Common emission patterns shared by multiple converters:

- Classification (rdf:type X via a reified Classification node)
- Ad-hoc ``ClassOf*`` minting (one per distinct kind label)
- Property + Scale + numeric value
- PropertyRange with lower / upper bounds

Each helper writes triples to a passed-in ``rdflib.Graph`` and returns the
URI of the node it created (or the existing URI for a memoised entity).
"""

from __future__ import annotations

from rdflib import Graph, Literal, RDF, RDFS, URIRef, XSD

from src.classify_part2 import owl_props as P
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext, slugify
from src.classify_part2.units import normalise_unit


def classification(
    g: Graph,
    *,
    ext_ns,
    classifier: URIRef,
    classified: URIRef,
    suffix: str,
) -> URIRef:
    """Emit a reified ``iso15926:Classification`` linking classifier to classified.

    Use ``suffix`` to make the Classification URI unique within this doc
    (typically the slug of the classified entity).
    """
    uri = mint_ext(ext_ns, kind="cls-link", ident=suffix)
    g.add((uri, RDF.type, ISO15926.Classification))
    g.add((uri, P.CLASSIFICATION_CLASSIFIER, classifier))
    g.add((uri, P.CLASSIFICATION_CLASSIFIED, classified))
    return uri


def mint_class_of(
    g: Graph,
    *,
    ext_ns,
    label: str,
    metaclass: URIRef,
    seen: dict[str, URIRef],
    parent: URIRef | None = None,
    comment: str | None = None,
    evidence: str | None = None,
) -> URIRef:
    """Mint (or reuse) an ad-hoc ``ClassOf*`` subclass keyed by label.

    *seen* is the per-document memo dict that the caller passes in;
    duplicate labels return the same URI without emitting twice.
    """
    key = slugify(label)
    if key in seen:
        return seen[key]
    uri = mint_ext(ext_ns, kind="cls", ident=key)
    g.add((uri, RDF.type, metaclass))
    g.add((uri, RDFS.label, Literal(label)))
    if parent is not None:
        g.add((uri, RDFS.subClassOf, parent))
    if comment:
        g.add((uri, RDFS.comment, Literal(comment)))
    if evidence:
        g.add((uri, DG.evidence, Literal(evidence)))
    seen[key] = uri
    return uri


def mint_scale(
    g: Graph,
    *,
    ext_ns,
    raw_unit: str | None,
    seen: dict[str, URIRef],
) -> URIRef | None:
    """Mint (or reuse) an ``iso15926:Scale`` for a unit string.

    Returns None for empty / dimensionless units; the converter then
    skips emitting the Scale link.
    """
    canonical = normalise_unit(raw_unit)
    if not canonical:
        return None
    if canonical in seen:
        return seen[canonical]
    uri = mint_ext(ext_ns, kind="scale", ident=canonical)
    g.add((uri, RDF.type, ISO15926.Scale))
    g.add((uri, RDFS.label, Literal(canonical)))
    seen[canonical] = uri
    return uri


def add_property_range(
    g: Graph,
    *,
    ext_ns,
    prop: URIRef,
    minimum: str | None,
    maximum: str | None,
) -> URIRef:
    """Emit a ``PropertyRange`` with optional lower / upper bounds.

    Returns the range URI. Bounds present only when their value is set —
    a one-sided bound is fine (lower-only or upper-only).
    """
    rng = mint_ext(ext_ns, kind="prng", ident=str(prop).rsplit("/", 1)[-1])
    g.add((rng, RDF.type, ISO15926.PropertyRange))

    if minimum is not None:
        lo = mint_ext(ext_ns, kind="lb", ident=str(prop).rsplit("/", 1)[-1])
        g.add((lo, RDF.type, ISO15926.LowerBoundOfPropertyRange))
        g.add((lo, ISO15926.hasContent, Literal(minimum)))
        g.add((rng, DG.lowerBound, lo))
    if maximum is not None:
        hi = mint_ext(ext_ns, kind="ub", ident=str(prop).rsplit("/", 1)[-1])
        g.add((hi, RDF.type, ISO15926.UpperBoundOfPropertyRange))
        g.add((hi, ISO15926.hasContent, Literal(maximum)))
        g.add((rng, DG.upperBound, hi))

    return rng
