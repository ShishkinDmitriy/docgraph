
from ..models import DocumentHit

import logging
from ..shape_extractor import (
    find_extraction_shape,
    shape_to_template,
)

logger = logging.getLogger(__name__)

GET_SHAPE_TOOL: dict = {
    "name": "get_shape",
    "description": (
        "Get the extraction template (SHACL shape) for any class — both document classes "
        "and nested entity classes such as foaf:Organization or foaf:Person. "
        "Call this to learn a child entity's properties before resolving it with find_entity. "
        "For the root document class, also provide confidence (0.0–1.0) and a one-sentence reason."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_uri": {
                "type": "string",
                "description": "Full URI of the document class, e.g. http://example.org/financial/DemandForPayment",
            },
            "confidence": {
                "type": "number",
                "description": "Classification confidence 0.0–1.0",
            },
            "reason": {
                "type": "string",
                "description": "One sentence explaining why this class was chosen",
            },
        },
        "required": ["class_uri"],
    },
}

def get_shape(
    self,
    class_uri: str,
    confidence: float | None = None,
    reason: str | None = None,
) -> dict:
    notation = self._uri_to_notation.get(class_uri)

    # Non-document classes (e.g. foaf:Organization resolved as a child entity)
    # are valid shape targets — just skip the classification-hit bookkeeping.
    if notation is not None:
        if self._hit is None:
            self._hit = DocumentHit(
                category=notation,
                class_uri=class_uri,
                confidence=float(confidence) if confidence is not None else 1.0,
                reason=reason or "",
            )
            if self._on_classified:
                self._on_classified(self._hit)
        elif confidence is not None:
            self._hit.confidence = float(confidence)
            self._hit.reason = reason or self._hit.reason
        logger.info("agent | classified: %s (%.0f%%)", notation,
                    self._hit.confidence * 100)

    shape_uri = find_extraction_shape(self.graph, class_uri)
    if shape_uri is None:
        return {"error": f"no extraction shape for {class_uri}"}

    # Use the class URI as node_uri placeholder — the root @id will be RESOLVE:
    # so the LLM fills it with the URI returned by find_entity.
    # All nested objects are either inline (no @id) or secondary (RESOLVE: already),
    # so the placeholder value is never visible in the final output.
    template = shape_to_template(
        self.graph, self.graph, shape_uri, class_uri,
        type_override=class_uri,
    )
    # Root @id: RESOLVE — the LLM fills it with suggested_uri from find_entity.
    template["@id"] = "<RESOLVE>"

    logger.debug("agent | shape: %d template keys", len(template))
    return {"template": template}