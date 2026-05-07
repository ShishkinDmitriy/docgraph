"""Template engine — Part 7-style lifted/lowered patterns.

See ARCHITECTURE.md > "Templates" for the design.
"""

from src.templates.loader import Slot, Template, load_template
from src.templates.expand import expand, materialize_lifted
from src.templates.recognize import recognize, to_sparql

__all__ = [
    "Slot", "Template", "load_template",
    "expand", "materialize_lifted",
    "recognize", "to_sparql",
]
