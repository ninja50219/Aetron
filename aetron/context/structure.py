"""The shape of one file, without any of its code.

Level 2 of the retrieval protocol. The model has picked a candidate out of a
search and needs to decide one thing: is the answer in this file, or was the
guess wrong? A skeleton answers that - every definition, where it starts and
ends, what it takes and what its own docstring claims it does - at a fraction
of the cost of the file itself.

The saving is the point. A wrong guess costs one skeleton rather than one file,
which is what makes it affordable for the model to be wrong, and being able to
afford being wrong is what lets it search at all.

Nothing here reads source. Everything comes from the index, which was built
once; a skeleton is a projection of ``FileSymbols``, not a second parse.

Two rules this level inherits:

*Say when there is nothing to say.* A file whose language has no parser
produces an empty skeleton, and an empty skeleton that does not explain itself
reads exactly like a file with no definitions in it. The two are opposite
findings and must never look alike.

*Docstrings are summarised, not reproduced.* A first line is a description; a
full docstring is prose that can run to a page, and at that point the skeleton
stops being cheaper than the code.
"""

import difflib
from dataclasses import asdict, dataclass, field

from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.symbols import FileSymbols, Symbol, SymbolKind
from aetron.scanner.scanner import ScanResult

# A docstring is included as its first line only. Long enough to say what a
# function is for, short enough that a hundred of them still fit.
SUMMARY_LIMIT = 120


@dataclass
class SymbolOutline:
    """One definition, reduced to what identifies it."""

    name: str
    kind: str
    line: int
    end_line: int
    qualified_name: str
    parameters: list[str] = field(default_factory=list)
    bases: list[str] = field(default_factory=list)
    summary: str = ""


@dataclass
class FileStructure:
    """Everything known about a file except what it says."""

    rel_path: str
    language: str
    lines: int = 0
    # What the file says it is for: its module docstring, first line only.
    summary: str = ""
    symbols: list[SymbolOutline] = field(default_factory=list)
    # Modules this file imports, as written in the source.
    imports: list[str] = field(default_factory=list)
    # Project files this file imports, resolved. Where to look next when the
    # answer turns out to be one level further in.
    imports_files: list[str] = field(default_factory=list)
    # Project files importing this one.
    imported_by: list[str] = field(default_factory=list)
    # Why the skeleton is empty, when it is. "" when the file parsed normally.
    unavailable: str = ""

    @property
    def available(self) -> bool:
        return not self.unavailable

    def to_dict(self) -> dict:
        """The JSON form handed to a model.

        Empty and duplicated fields are dropped. This is not tidiness: the
        skeleton exists to be cheaper than the file, and a qualified name that
        merely repeats the name costs tokens to say nothing.
        """
        data = asdict(self)
        data["symbols"] = [
            {
                key: value
                for key, value in symbol.items()
                if value not in ([], "", 0)
                and not (key == "qualified_name" and value == symbol["name"])
            }
            for symbol in data["symbols"]
        ]
        return {key: value for key, value in data.items() if value not in ([], "", 0)}


def _render_symbol(symbol: SymbolOutline) -> str:
    signature = symbol.qualified_name
    if symbol.parameters:
        signature = f"{signature}({', '.join(symbol.parameters)})"
    elif symbol.kind in ("function", "method"):
        signature = f"{signature}()"
    if symbol.bases:
        signature = f"{signature}({', '.join(symbol.bases)})"

    line = f"  {symbol.kind} {signature} @{symbol.line}-{symbol.end_line}"
    return f"{line}  {symbol.summary}" if symbol.summary else line


def summarise(docstring: str | None) -> str:
    """The first line of a docstring, truncated.

    The first line is the convention for a one-line description in every
    language that has docstrings at all, so this needs no language knowledge.
    """
    if not docstring:
        return ""

    first = docstring.strip().split("\n", 1)[0].strip()
    if len(first) <= SUMMARY_LIMIT:
        return first
    return first[: SUMMARY_LIMIT - 1].rstrip() + "…"


def outline(symbol: Symbol) -> SymbolOutline:
    return SymbolOutline(
        name=symbol.name,
        kind=symbol.kind.value,
        line=symbol.line,
        end_line=symbol.end_line,
        qualified_name=symbol.qualified_name or symbol.name,
        parameters=list(symbol.parameters),
        bases=list(symbol.bases),
        summary=summarise(symbol.docstring),
    )


def _import_names(file_symbols: FileSymbols) -> list[str]:
    """Imports as a reader would write them, relative dots included."""
    names = []
    for reference in file_symbols.imports:
        prefix = "." * reference.level
        module = f"{prefix}{reference.module}"
        if reference.from_import and reference.names:
            module = f"{module} ({', '.join(reference.names)})"
        names.append(module)
    return list(dict.fromkeys(names))


def build_structure(
    scan_result: ScanResult, analysis: AnalysisResult, rel_path: str
) -> FileStructure:
    """The skeleton of one file.

    ``rel_path`` is a path as the scanner records it, which is exactly what a
    search result hands back, so the model never has to construct one. Takes
    the scan alongside the analysis for the same reason search does: the scan
    knows about every file, the analysis only about the ones it could read.
    """
    scanned = next((f for f in scan_result.files if f.rel_path == rel_path), None)

    structure = FileStructure(
        rel_path=rel_path,
        language=scanned.language if scanned else "unknown",
        lines=scanned.lines if scanned else 0,
    )

    found = next((f for f in analysis.files if f.rel_path == rel_path), None)

    if found is None:
        # Either the file is not in the project, or its language has no parser.
        # Those are different answers and the model acts differently on each.
        if rel_path in analysis.unparsed:
            structure.unavailable = (
                "no parser for this language yet; the file was scanned but not read"
            )
        else:
            structure.unavailable = _not_found(scan_result, rel_path)
        return structure

    if found.parse_error:
        structure.unavailable = f"could not be parsed: {found.parse_error}"
        return structure

    module = next(
        (s for s in found.symbols if s.kind == SymbolKind.MODULE), None
    )
    if module is not None:
        structure.summary = summarise(module.docstring)

    structure.symbols = [
        outline(symbol)
        for symbol in found.symbols
        if symbol.kind != SymbolKind.MODULE
    ]
    structure.imports = _import_names(found)
    structure.imports_files = sorted(analysis.imports.get(rel_path, ()))
    structure.imported_by = sorted(analysis.imported_by.get(rel_path, ()))
    return structure


def render(structure: FileStructure) -> str:
    """The skeleton as text, for a reader that is not a JSON parser.

    JSON repeats its keys for every symbol, and a file of many small
    definitions has more keys than code - measured on this project, the JSON
    skeleton of a file that is almost entirely signatures is *larger* than the
    file. Naming each field once in a header and then writing one line per
    definition costs a fraction of that, and a model reads it at least as
    easily. JSON remains the form for anything that parses rather than reads.
    """
    if not structure.available:
        return f"{structure.rel_path}\n  unavailable: {structure.unavailable}"

    header = f"{structure.rel_path}  {structure.language}"
    if structure.lines:
        header = f"{header}  {structure.lines} lines"

    parts = [header]
    if structure.summary:
        parts.append(f"  {structure.summary}")
    parts.extend(_render_symbol(symbol) for symbol in structure.symbols)

    if not structure.symbols:
        parts.append("  no definitions in this file")
    if structure.imports:
        parts.append(f"  imports: {', '.join(structure.imports)}")
    if structure.imported_by:
        parts.append(f"  imported by: {', '.join(structure.imported_by)}")

    return "\n".join(parts)


# How many near misses to offer. One is usually right and four is a list to
# read rather than an answer.
SUGGESTION_LIMIT = 3


def _not_found(scan_result: ScanResult, rel_path: str) -> str:
    """Say the file is not here, and name the ones it was probably meant to be.

    A path is easy to get almost right in a project of any size - a directory
    left out, a name remembered without its folder - and "not in this project's
    index" is true but leaves the caller no better off. A model reading this is
    in exactly that position and can act on a name.
    """
    known = [f.rel_path for f in scan_result.files]
    wanted = rel_path.replace("\\", "/").strip("/")
    base = wanted.rsplit("/", 1)[-1]

    # A file whose name matches exactly and whose directory does not is the
    # common case, and closer to what was meant than any string distance.
    same_name = [path for path in known if path.rsplit("/", 1)[-1] == base]
    close = difflib.get_close_matches(wanted, known, n=SUGGESTION_LIMIT, cutoff=0.6)

    suggestions = list(dict.fromkeys([*same_name, *close]))[:SUGGESTION_LIMIT]
    if not suggestions:
        return "not a file in this project's index"

    return (
        "not a file in this project's index; did you mean "
        + ", ".join(suggestions)
        + "?"
    )
