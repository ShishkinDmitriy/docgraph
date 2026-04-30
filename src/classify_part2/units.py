"""Unit-string normalisation.

The LLM emits units verbatim from the document (m^3/h, degC, …). Before
minting an ``iso15926:Scale`` URI, we normalise to a canonical form so
equivalent unit strings share one Scale node.
"""

_NORMALISATIONS = {
    "m^3/h":  "m³/h",
    "m3/h":   "m³/h",
    "m^3":    "m³",
    "m3":     "m³",
    "m^2":    "m²",
    "m2":     "m²",
    "degC":   "°C",
    "deg C":  "°C",
    "degc":   "°C",
    "C°":     "°C",
    "degF":   "°F",
    "deg F":  "°F",
    "degf":   "°F",
    "F°":     "°F",
    "degK":   "K",
    "deg K":  "K",
    "ohm":    "Ω",
    "ohms":   "Ω",
    "micro":  "µ",
    "u":      "µ",   # only when standalone — handled by full-string match
    "%%":     "%",
    "deg":    "°",
}


def normalise_unit(unit: str | None) -> str:
    """Return a canonical unit string. Empty or None → ""."""
    if not unit:
        return ""
    s = unit.strip()
    return _NORMALISATIONS.get(s, s)
