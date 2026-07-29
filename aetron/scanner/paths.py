"""Turning user-typed text into a usable project path.

Shared by every front end: the CLI takes the string from argv or a prompt, a
GUI would take it from a text field or a folder picker. The rules are the same.
"""

from pathlib import Path


class InvalidPathError(ValueError):
    """Raised when user input cannot be used as a project directory."""


def normalize_path(raw: str) -> Path:
    """Clean up user input and return an absolute, existing directory.

    Handles the things people actually type: surrounding quotes (Windows
    "Copy as path" adds them), "~" for the home directory, stray whitespace.
    """
    text = raw.strip()

    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()

    if not text:
        raise InvalidPathError("No path given.")

    path = Path(text).expanduser()

    try:
        path = path.resolve()
    except OSError as exc:
        raise InvalidPathError(f"Cannot resolve path: {exc.strerror}") from exc

    if not path.exists():
        raise InvalidPathError(f"Path does not exist: {path}")

    if not path.is_dir():
        raise InvalidPathError(f"Not a directory: {path}")

    return path
