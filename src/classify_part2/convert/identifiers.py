"""Convert prompt #12 (Identifiers & descriptions).

Two emission modes depending on ``representation_kind``:

- ``name`` / ``alias`` / ``description`` are *shortcuts* — direct
  ``rdfs:label`` / ``skos:altLabel`` / ``rdfs:comment`` triples on the
  target individual, no reified node. This avoids the duplication where
  the same alias appeared both as ``skos:altLabel`` (from prompt #3) and
  as a separate Identification (from prompt #12).

- ``identifier`` / ``definition`` / ``cross_reference`` use the full
  Part 2 reification:

      <sign>  a  iso15926:WholeLifeIndividual,  <form-class> .   # the actual text
      <form-class>  a  iso15926:ClassOfInformationRepresentation .
      <rep>  a  iso15926:Identification ;
          iso15926:hasSign        <sign> ;
          iso15926:hasRepresented <target> .

  The form-class is minted once per distinct ``system`` value (one shared
  ``iban`` form-class across every IBAN in the document, etc.).
"""

from __future__ import annotations

from rdflib import Graph, Literal, Namespace, RDF, RDFS

from src.classify_part2 import owl_props as P
from src.classify_part2 import reify
from src.classify_part2.context import ConversionContext, EntityRef
from src.classify_part2.ns import DG, ISO15926
from src.classify_part2.uri import mint_ext, slugify

SKOS = Namespace("http://www.w3.org/2004/02/skos/core#")

# kinds that get a reified relationship node (sign + Identification-class triple)
_REIFIED_KINDS = {
    "identifier":      ISO15926.Identification,
    "definition":      ISO15926.Definition,
    "cross_reference": ISO15926.RepresentationOfThing,
}


def convert(data: dict, ctx: ConversionContext) -> Graph:
    g = Graph()
    g.bind("skos", SKOS)
    for entry in data.get("representations") or []:
        _emit(g, entry, ctx)
    return g


def _emit(g: Graph, entry: dict, ctx: ConversionContext) -> None:
    rid       = entry.get("id")
    rep_kind  = entry.get("representation_kind")
    target_id = entry.get("represents")
    value     = entry.get("value")
    if not (rid and rep_kind and target_id and value is not None):
        return
    target = ctx.get(target_id)
    if target is None:
        return

    if rep_kind == "name":
        # The primary label probably already exists from prompt #3; use
        # skos:prefLabel here so it's visible without overwriting label.
        g.add((target.uri, SKOS.prefLabel, Literal(str(value))))
        return
    if rep_kind == "alias":
        g.add((target.uri, SKOS.altLabel, Literal(str(value))))
        return
    if rep_kind == "description":
        g.add((target.uri, RDFS.comment, Literal(str(value))))
        return

    # Reified path — identifier, definition, cross_reference.
    cls = _REIFIED_KINDS.get(rep_kind, ISO15926.RepresentationOfThing)
    uri = mint_ext(ctx.ext_ns, kind="rep", ident=rid)
    g.add((uri, RDF.type, cls))
    g.add((uri, P.REPR_REPRESENTED, target.uri))

    if rep_kind == "cross_reference":
        # Cross-reference targets live outside our graph — keep the
        # external pointer as a literal rather than minting a sign.
        g.add((uri, DG.externalRef, Literal(str(value))))
    else:
        # Sign-side mints a possible_individual carrying the actual
        # text (Part 2 §5.2.16). Its form-class (e.g. ext:cls/iban)
        # encodes the naming system — no separate dg:system literal
        # needed.
        sign_uri = _emit_sign(g, ctx, value=str(value), system=entry.get("system"))
        g.add((uri, P.REPR_SIGN, sign_uri))

    if (desc := entry.get("description")):
        g.add((uri, RDFS.comment, Literal(desc)))
    if (evidence := entry.get("evidence")):
        g.add((uri, DG.evidence, Literal(evidence)))

    ctx.register(EntityRef(id=rid, kind="representation", uri=uri,
                           label=f"{rep_kind}: {value}"))


def _emit_sign(g: Graph, ctx: ConversionContext, *, value: str, system: str | None) -> "URIRef":
    """Mint the sign individual carrying the actual identifier text.

    Sign = an ``iso15926:WholeLifeIndividual`` whose ``rdfs:label`` holds
    the literal text. Each sign is also classified by a per-system
    ``ClassOfInformationRepresentation`` (one ``iban`` form-class shared
    by every IBAN in the document; one ``steuernummer`` shared by every
    German tax number; etc.).
    """
    sign_ident = f"{slugify(system)}-{slugify(value)}" if system else slugify(value)
    sign_uri = mint_ext(ctx.ext_ns, kind="sign", ident=sign_ident)
    g.add((sign_uri, RDF.type, ISO15926.WholeLifeIndividual))
    g.add((sign_uri, P.HAS_CONTENT, Literal(value)))
    g.add((sign_uri, RDFS.label,    Literal(value)))

    if system:
        form_cls = reify.mint_class_of(
            g, ext_ns=ctx.ext_ns,
            label=system,
            metaclass=ISO15926.ClassOfInformationRepresentation,
            seen=ctx.classes_minted,
        )
        g.add((sign_uri, RDF.type, form_cls))
    return sign_uri
