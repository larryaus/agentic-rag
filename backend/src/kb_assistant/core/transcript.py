from dataclasses import dataclass
from typing import Any


@dataclass
class UserText:
    text: str
    guard: bool = False


@dataclass
class TextBlock:
    block_index: int
    text: str


@dataclass
class ToolUseBlock:
    block_index: int
    id: str
    name: str
    input: dict[str, Any]
    input_error: str | None = None


@dataclass
class AssistantTurn:
    blocks: list[TextBlock | ToolUseBlock]

    def __post_init__(self) -> None:
        self.blocks.sort(key=lambda block: block.block_index)


@dataclass
class ToolResult:
    id: str
    ok: bool
    json_payload: dict[str, Any] | None = None
    text: str | None = None


@dataclass
class ToolResults:
    results: list[ToolResult]


Turn = UserText | AssistantTurn | ToolResults
