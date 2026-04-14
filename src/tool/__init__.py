from .find_entity_tool import FIND_ENTITY_TOOL, find_entity
from .get_shape_tool import GET_SHAPE_TOOL, get_shape
from .submit_extraction_tool import SUBMIT_EXTRACTION_TOOL
from .validate_tool import VALIDATE_TOOL, validate_tool

AGENT_TOOLS: list[dict] = [
    GET_SHAPE_TOOL,
    FIND_ENTITY_TOOL,
    VALIDATE_TOOL,
    SUBMIT_EXTRACTION_TOOL,
]
