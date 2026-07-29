"""Building a symbol index and dependency graph from a scan result.

This is the layer that lets later stages ask questions without re-reading the
whole project: what defines this name, what does this file depend on, what is
never referenced.
"""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aetron.scanner.scanner import ScanResult

from . import python_parser
from .deadcode import DeadCodeCandidate, find_dead_code
from .resolver import build_module_map, resolve
from .symbols import FileSymbols, Symbol, SymbolKind

# Language name -> parser. Adding a language means adding an entry here; the
# rest of this module does not change.
PARSERS: dict[str, Callable[[str, str], FileSymbols]] = {
    python_parser.LANGUAGE: python_parser.parse,
}

ProgressCallback = Callable[[int, str], None]


@dataclass
class AnalysisResult:
    root: Path
    files: list[FileSymbols] = field(default_factory=list)
    # Import edges: file -> the project files it imports.
    imports: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # Reverse of ``imports``, so "who uses this?" is a lookup, not a search.
    imported_by: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # Files whose language has no parser yet.
    unparsed: list[str] = field(default_factory=list)

    @property
    def all_symbols(self) -> list[tuple[str, Symbol]]:
        return [(f.rel_path, s) for f in self.files for s in f.symbols]

    @property
    def parse_errors(self) -> list[tuple[str, str]]:
        return [(f.rel_path, f.parse_error) for f in self.files if f.parse_error]

    def count_of(self, kind: SymbolKind) -> int:
        return sum(len(f.of_kind(kind)) for f in self.files)

    def find(self, name: str) -> list[tuple[str, Symbol]]:
        """Every definition of a name, by plain or qualified name."""
        return [
            (rel_path, symbol)
            for rel_path, symbol in self.all_symbols
            if name in (symbol.name, symbol.qualified_name)
        ]

    def entry_points(self) -> list[str]:
        """Files nothing else imports: where reading a project starts."""
        return sorted(f.rel_path for f in self.files if not self.imported_by[f.rel_path])

    def dead_code(self) -> list[DeadCodeCandidate]:
        """Definitions nothing appears to use. Computed on demand, not cached,
        because it is a report rather than part of the index."""
        return find_dead_code(self.files)


def analyze(
    scan_result: ScanResult, on_progress: ProgressCallback | None = None
) -> AnalysisResult:
    """Parse every scanned file that has a parser, then link them together."""
    result = AnalysisResult(root=scan_result.root)

    parsable = [f for f in scan_result.files if f.language in PARSERS]
    result.unparsed = sorted(
        f.rel_path for f in scan_result.files if f.language not in PARSERS
    )

    for position, file_info in enumerate(parsable, start=1):
        if on_progress is not None:
            on_progress(position, file_info.rel_path)

        try:
            text = file_info.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            result.files.append(
                FileSymbols(
                    rel_path=file_info.rel_path,
                    language=file_info.language,
                    parse_error=f"unreadable: {exc.strerror}",
                )
            )
            continue

        parser = PARSERS[file_info.language]
        result.files.append(parser(text, file_info.rel_path))

    _link_imports(result)
    return result


def _link_imports(result: AnalysisResult) -> None:
    """Resolve every import to a file in the project, where one exists."""
    modules = build_module_map([f.rel_path for f in result.files])

    for file_symbols in result.files:
        for reference in file_symbols.imports:
            target = resolve(reference, file_symbols.rel_path, modules)
            if target is None or target == file_symbols.rel_path:
                continue  # external package, or a module importing itself

            result.imports[file_symbols.rel_path].add(target)
            result.imported_by[target].add(file_symbols.rel_path)

    # Make sure every file has an entry, so lookups never surprise a caller.
    for file_symbols in result.files:
        result.imports.setdefault(file_symbols.rel_path, set())
        result.imported_by.setdefault(file_symbols.rel_path, set())
