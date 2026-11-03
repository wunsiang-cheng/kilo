"""Tool declaration shared by every built-in tool."""

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Tuple


@dataclass(frozen=True)
class Tool:
    """
    Everything Kilo knows about one built-in tool, declared in one place.
      - definition: OpenAI-style function definition (name, description, parameters) sent to the LLM
      - execute: coroutine taking the parsed arguments and returning a JSON string
      - aliases: alternative names the LLM may call the tool by
    """

    definition: Dict[str, Any]
    execute: Callable[[Dict[str, Any]], Awaitable[str]]
    aliases: Tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.definition["name"]
