"""Validate classified RDF results against SHACL shapes."""

from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, Namespace
from rdflib.namespace import RDF, SH


SH_VIOLATION = SH.Violation
SH_WARNING   = SH.Warning
SH_INFO      = SH.Info

_SEVERITY_LABEL = {
    str(SH_VIOLATION): "violation",
    str(SH_WARNING):   "warning",
    str(SH_INFO):      "info",
}


@dataclass
class ShapeViolation:
    focus_node: str
    result_path: str | None
    message: str
    severity: str             # "violation" | "warning" | "info"
    constraint_component: str # e.g. "MinCountConstraintComponent"

    @property
    def is_missing_field(self) -> bool:
        """True when the violation is a missing required value (sh:minCount)."""
        return "MinCountConstraintComponent" in self.constraint_component


def validate(data_path: Path, shapes_path: Path) -> list[ShapeViolation]:
    """
    Validate *data_path* against *shapes_path*.
    Returns a (possibly empty) list of ShapeViolation objects.
    Raises ImportError if pyshacl is not installed.
    """
    from pyshacl import validate as _shacl_validate

    data_graph = Graph()
    data_graph.parse(data_path)

    conforms, results_graph, _ = _shacl_validate(
        data_graph,
        shacl_graph=str(shapes_path),
        inference="none",
        abort_on_first=False,
    )

    if conforms:
        return []

    violations: list[ShapeViolation] = []
    for result_node in results_graph.subjects(RDF.type, SH.ValidationResult):
        focus     = results_graph.value(result_node, SH.focusNode)
        path      = results_graph.value(result_node, SH.resultPath)
        message   = results_graph.value(result_node, SH.resultMessage)
        severity  = results_graph.value(result_node, SH.resultSeverity)
        component = results_graph.value(result_node, SH.sourceConstraintComponent)

        violations.append(ShapeViolation(
            focus_node           = str(focus)     if focus     else "?",
            result_path          = str(path)      if path      else None,
            message              = str(message)   if message   else "constraint violated",
            severity             = _SEVERITY_LABEL.get(str(severity), "violation"),
            constraint_component = str(component) if component else "",
        ))

    return violations
