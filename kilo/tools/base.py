"""Tool declaration shared by every built-in tool, and what a tool returns."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Tuple, Union


@dataclass(frozen=True)
class ShownImage:
    """An image a tool result shows the model: a file in the image store (kilo.images), and what it is, for the model."""

    file: str
    label: str


@dataclass(frozen=True)
class ToolResult:
    """A tool's result: its JSON text, and the images it shows the model (in a message after the tool results)."""

    text: str
    images: Tuple[ShownImage, ...] = ()


@dataclass(frozen=True)
class Tool:
    """
    Everything Kilo knows about one built-in tool, declared in one place.
      - definition: OpenAI-style function definition (name, description, parameters) sent to the LLM
      - execute: coroutine taking the parsed arguments and returning a JSON string, or a ToolResult when the
        result shows images
      - aliases: alternative names the LLM may call the tool by
    """

    definition: Dict[str, Any]
    execute: Callable[[Dict[str, Any]], Awaitable[Union[str, ToolResult]]]
    aliases: Tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.definition["name"]
