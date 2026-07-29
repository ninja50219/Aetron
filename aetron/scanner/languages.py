"""Mapping from file extensions to language names."""

EXTENSION_MAP = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
    ".lua": "lua",
    ".scala": "scala",
    ".dart": "dart",
}


def detect_language(suffix: str) -> str | None:
    """Return the language for a file extension, or None if unsupported."""
    return EXTENSION_MAP.get(suffix.lower())
