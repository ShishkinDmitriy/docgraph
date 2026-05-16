"""Namespace constants used across the classify pipeline."""

from rdflib import Namespace

DG       = Namespace("urn:docgraph:vocab:meta#")
ISO15926 = Namespace("http://rds.posccaesar.org/2008/02/OWL/ISO-15926-2_2003#")
EXT_BASE = "urn:docgraph:extraction"


def EXT_NS_FOR(slug: str) -> Namespace:
    """Per-source extraction namespace: <EXT_BASE>/<slug>/."""
    return Namespace(f"{EXT_BASE}/{slug}/")
