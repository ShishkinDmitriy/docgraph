
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