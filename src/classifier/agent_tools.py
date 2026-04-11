"""Tool schema definitions for the extraction agent."""

CLASSIFY_TOOL: dict = {
    "name": "submit_classification",
    "description": "Submit the single most relevant document type for this document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category":   {"type": "string", "description": "skos:notation of the matched class"},
            "class_uri":  {"type": "string", "description": "Full URI of the matched OWL class"},
            "confidence": {"type": "number", "description": "0.0–1.0"},
            "reason":     {"type": "string", "description": "One sentence"},
        },
        "required": ["category", "class_uri", "confidence", "reason"],
    },
}

GET_SHAPE_TOOL: dict = {
    "name": "get_shape",
    "description": (
        "Get the extraction template for a document class. "
        "Call this first after reading the @type hint and choosing the class. "
        "The template shows all properties to extract; the root @id is still RESOLVE: — "
        "resolve it with find_entity after seeing which fields are available. "
        "Provide confidence (0.0–1.0) and a one-sentence reason for the classification."
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

FIND_ENTITY_TOOL: dict = {
    "name": "find_entity",
    "description": (
        "Search the knowledge graph for an existing entity. "
        "Returns matches (each with uri and known_properties), suggested_uri (stable URI "
        "to use when matches is empty), and available_properties (the exact property CURIEs "
        "defined in the schema for this class — use these as search keys, not guesses). "
        "Use for every object whose @id starts with '<RESOLVE', and for the root "
        "document stub before filling the template. "
        "If the document contains properties not in known_properties, include them in the "
        "extracted document — they will be merged as new facts. "
        "A differing value (e.g. a new address) is not an error — both values will be kept."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "class_uri": {
                "type": "string",
                "description": "Class URI or CURIE, e.g. foaf:Organization",
            },
            "properties": {
                "type": "object",
                "description": (
                    "Property-value pairs to match. Keys are CURIEs or slash-separated "
                    "property paths for nested values. "
                    "e.g. {\"fin:taxId\": \"7713759202\"} or {\"foaf:name\": \"ООО Дельта\"} "
                    "or {\"fin:issuer/foaf:name\": \"Zahnarztpraxis Liebermann\"}. "
                    "Fewer, more specific properties yield better results."
                ),
            },
        },
        "required": ["class_uri", "properties"],
    },
}

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

SUBMIT_EXTRACTION_TOOL: dict = {
    "name": "submit_extraction",
    "description": (
        "Submit the final extracted document. "
        "Call when validation passes or after two validation attempts."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "document": {
                "type": "object",
                "description": "Final filled JSON-LD document with all entity references resolved.",
                "properties": {
                    "@id":   {"type": "string"},
                    "@type": {"type": "string"},
                },
                "required": ["@id", "@type"],
                "additionalProperties": True,
            },
        },
        "required": ["document"],
    },
}

AGENT_TOOLS: list[dict] = [
    GET_SHAPE_TOOL,
    FIND_ENTITY_TOOL,
    VALIDATE_TOOL,
    SUBMIT_EXTRACTION_TOOL,
]
