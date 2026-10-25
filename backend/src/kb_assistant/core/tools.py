from typing import Any

SEARCH_TOOL: dict[str, Any] = {
    "name": "search_knowledge_base",
    "description": "Search the enterprise knowledge base for passages relevant to a question. Always call this before making any factual claim about company documents.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A focused standalone search query. Strip pleasantries and pronouns.",
            }
        },
        "required": ["query"],
    },
}
