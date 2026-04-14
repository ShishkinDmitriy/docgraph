
import json
import logging
from rdflib import Graph, Namespace, RDF, URIRef
from rdflib.namespace import RDF as RDF_NS, RDFS
from .. import ontology as _ontology
from ..ontology import JSONLD_CONTEXT, prefixed_name

logger = logging.getLogger(__name__)

VALIDATE_TOOL: dict = {
    "name": "validate",
    "description": (
        "Validate the extracted document against its SHACL shape. "
        "Returns a list of violations; an empty list means the document is valid. "
        "You may call this at most twice."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document": {
                "type": "object",
                "description": "The filled JSON-LD document to validate (no @context needed).",
            },
        },
        "required": ["document"],
    },
}

def validate_tool(self, document: dict) -> dict:
    if self._validate_count >= 2:
        return {"violations": [], "note": "max attempts reached"}
    self._validate_count += 1

    data_graph = Graph()
    jsonld = {**document, "@context": JSONLD_CONTEXT}
    try:
        data_graph.parse(
            data=json.dumps(jsonld, ensure_ascii=False), format="json-ld"
        )
    except Exception as exc:
        return {"violations": [{"message": f"parse error: {exc}"}]}

    try:
        from pyshacl import validate as shacl_validate
        from rdflib.namespace import SH as SH_NS

        conforms, results_graph, _ = shacl_validate(
            data_graph,
            shacl_graph=self.graph,
            inference="none",
            abort_on_first=False,
        )
        if conforms:
            return {"violations": []}

        violations = []
        for node in results_graph.subjects(RDF_NS.type, SH_NS.ValidationResult):
            path     = results_graph.value(node, SH_NS.resultPath)
            message  = results_graph.value(node, SH_NS.resultMessage)
            severity = results_graph.value(node, SH_NS.resultSeverity)
            violations.append({
                "path":     str(path).rsplit("#", 1)[-1] if path else None,
                "message":  str(message) if message else "constraint violated",
                "severity": str(severity).rsplit("#", 1)[-1] if severity else "Violation",
            })
        logger.debug("agent | validate: %d violation(s)", len(violations))
        return {"violations": violations}
    except ImportError:
        return {"violations": [], "note": "pyshacl not installed"}
    except Exception as exc:
        logger.warning("agent | validate error: %s", exc)
        return {"violations": [], "error": str(exc)}