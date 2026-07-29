"""Turning import statements into edges between files.

An import names a module; a dependency graph needs a file. Only imports that
resolve to a file inside the scanned tree become edges — "import os" is real,
but it points outside the project and says nothing about its structure.
"""

from .symbols import ImportRef

PACKAGE_INIT = "__init__.py"


def module_name(rel_path: str) -> str:
    """The dotted module name a Python file provides.

    ``aetron/scanner/ignore.py`` -> ``aetron.scanner.ignore``
    ``aetron/scanner/__init__.py`` -> ``aetron.scanner``
    """
    parts = rel_path.replace("\\", "/").split("/")

    if parts[-1] == PACKAGE_INIT:
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].rsplit(".", 1)[0]

    return ".".join(part for part in parts if part)


def build_module_map(rel_paths: list[str]) -> dict[str, str]:
    """Map every module name in the project to the file that defines it.

    A file also answers to its trailing name segments, so an import written
    from inside a package ("from scanner import ignore") resolves even though
    the scan root sits above the package.
    """
    modules: dict[str, str] = {}

    for rel_path in rel_paths:
        if not rel_path.replace("\\", "/").endswith(".py"):
            continue

        full = module_name(rel_path)
        if not full:
            continue

        modules.setdefault(full, rel_path)

        parts = full.split(".")
        for start in range(1, len(parts)):
            # Longer, more specific names are registered first, so a suffix
            # never displaces a full match.
            modules.setdefault(".".join(parts[start:]), rel_path)

    return modules


def resolve(reference: ImportRef, source_rel_path: str, modules: dict[str, str]) -> str | None:
    """Return the file an import points at, or None if it leaves the project."""
    target = _absolute_module(reference, source_rel_path)
    if not target:
        return None

    # "from package import module" names a submodule, and that submodule is
    # the real target - checking it before the package itself keeps the edge
    # from collapsing onto __init__.py.
    if reference.from_import:
        for name in reference.names:
            hit = modules.get(f"{target}.{name}")
            if hit is not None:
                return hit

    return modules.get(target)


def _absolute_module(reference: ImportRef, source_rel_path: str) -> str:
    """Expand a relative import against the package holding the source file."""
    if not reference.is_relative:
        return reference.module

    package = module_name(source_rel_path)
    parts = package.split(".") if package else []

    # A module inside a package: one dot means the package itself, so the
    # module's own name comes off first.
    if not source_rel_path.replace("\\", "/").endswith(PACKAGE_INIT) and parts:
        parts = parts[:-1]

    # Each extra dot climbs one more level.
    climb = reference.level - 1
    if climb:
        parts = parts[:-climb] if climb <= len(parts) else []

    if reference.module:
        parts = [*parts, *reference.module.split(".")]

    return ".".join(part for part in parts if part)
