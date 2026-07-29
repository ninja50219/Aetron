"""The vocabulary every language parser produces.

Language-specific parsing happens elsewhere; these types are what all of them
agree on, so the index and everything downstream never needs to know which
language a symbol came from.
"""

from dataclasses import dataclass, field
from enum import Enum


class SymbolKind(str, Enum):
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"


@dataclass
class Symbol:
    """A named definition found in a source file."""

    name: str
    kind: SymbolKind
    line: int
    end_line: int
    # Dotted path within the file, e.g. "Scanner.scan" for a method.
    qualified_name: str = ""
    # Class names a class inherits from, as written in the source.
    bases: list[str] = field(default_factory=list)
    # Parameter names, for functions and methods.
    parameters: list[str] = field(default_factory=list)
    docstring: str | None = None

    @property
    def is_callable(self) -> bool:
        return self.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD)


@dataclass
class ImportRef:
    """An import statement, before it is resolved to a file.

    ``module`` is the module as written ("os.path", ".ignore"). ``level`` is the
    number of leading dots in a relative import, 0 for an absolute one.
    ``from_import`` marks the "from X import Y" form, where each name may be a
    submodule rather than a symbol.
    """

    module: str
    names: list[str] = field(default_factory=list)
    level: int = 0
    line: int = 0
    from_import: bool = False

    @property
    def is_relative(self) -> bool:
        return self.level > 0


@dataclass
class FileSymbols:
    """Everything one parser extracted from one file."""

    rel_path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[ImportRef] = field(default_factory=list)
    # Every name this file reads: variables, calls, attributes, base classes.
    # Deliberately a flat set rather than a per-scope structure - the question
    # it answers ("is this name used anywhere?") does not need scopes, and a
    # flat set keeps false "unused" reports down.
    references: set[str] = field(default_factory=set)
    # Set when the file could not be parsed; symbols and imports stay empty.
    parse_error: str | None = None

    def of_kind(self, kind: SymbolKind) -> list[Symbol]:
        return [s for s in self.symbols if s.kind == kind]
