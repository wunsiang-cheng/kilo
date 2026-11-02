"""The agent's workspace: one fixed working directory, where relative paths in every tool resolve."""

from pathlib import Path

from kilo.config import settings


def root() -> Path:
    return settings.kilo_workspace


def resolve(path_str: str) -> Path:
    """Resolve a tool's path argument: absolute paths as given, relative paths inside the workspace."""
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = root() / path
    return path.resolve()


def display(path: Path) -> str:
    """A path for tool output: relative to the workspace when inside it, absolute otherwise."""
    try:
        return str(path.relative_to(root().resolve()))
    except ValueError:
        return str(path)


def prompt_section() -> str:
    """System-prompt text describing the workspace."""
    return f"""Workspace:
Your working directory is `{root()}`. Relative paths in file tools, `glob`, `grep` and `shell` resolve there; absolute paths work anywhere.
"""
