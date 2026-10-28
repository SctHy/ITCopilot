"""Tool contracts shared by application registration and evaluation scripts."""

KNOWLEDGE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "minLength": 1},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
        "department": {"type": ["string", "null"]},
        "mode": {"type": "string", "enum": ["hybrid", "vector", "bm25"]},
    },
    "required": ["query"],
    "additionalProperties": False,
}
