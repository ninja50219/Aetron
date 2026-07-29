"""Recognising documentation files.

These never go to an AST parser, but they are the best short description of
what a project is for, so they are collected separately from source code.
Keeping them out of ``ScanResult.files`` keeps line counts and the future
dependency graph honest.
"""

DOC_SUFFIXES = {".md", ".rst", ".adoc", ".txt"}

# Matched without an extension, so both README.md and plain README count.
DOC_STEMS = {
    "readme",
    "contributing",
    "changelog",
    "license",
    "licence",
    "authors",
    "notice",
    "security",
    "code_of_conduct",
    "architecture",
    "roadmap",
}

DOC_DIRS = {"docs", "doc", "documentation"}


def is_doc_file(name: str, rel_path: str = "") -> bool:
    """True for a documentation file, by well-known name or by living in docs/."""
    lowered = name.lower()
    stem, _, suffix = lowered.rpartition(".")
    suffix = f".{suffix}" if stem else ""
    stem = stem or lowered

    if stem in DOC_STEMS:
        return True

    if suffix in DOC_SUFFIXES:
        parts = rel_path.replace("\\", "/").lower().split("/")[:-1]
        return any(part in DOC_DIRS for part in parts)

    return False
