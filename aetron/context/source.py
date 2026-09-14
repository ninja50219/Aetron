"""The code of one definition, and nothing around it.

Level 3, and the only level where source leaves the project. Everything above
exists so that this one is reached rarely and, when it is reached, asked a
narrow question: not "show me LoginController.cs" but "show me LoginHandler",
which the index already knows spans lines 68 to 91.

The index makes that a slice rather than a second parse, and the difference is
the whole economy of the protocol: a method in a four thousand line file costs
its own twenty lines.

The awkward part of this level is that it is the first to touch the disk since
the scan. An index is a photograph, and the file may have been edited since it
was taken. Returning lines 68-91 of a file that has changed underneath is the
one way this design can quietly mislead: the code would look authoritative and
be wrong. So the slice is checked against what the index expects to find there,
and a mismatch is reported rather than smoothed over.
"""

import difflib
from dataclasses import dataclass
from pathlib import Path

from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.symbols import Symbol
from aetron.scanner.detect import read_source
from aetron.scanner.scanner import ScanResult

# How many attached lines above a definition to keep. Decorators and the
# comment written directly above a definition belong to it - returning a def
# line without the decorator that changed its meaning would be misleading - but
# only those. Counting back a fixed number of lines instead reaches into
# whatever the previous definition happened to end with.
LEAD_LINES = 3

# Line prefixes that attach to the definition below them rather than standing
# on their own: decorators and comments, in the styles the scanner's languages
# write them.
ATTACHED_PREFIXES = ("@", "#", "//", "/*", "*", "///", "<!--")


@dataclass
class SourceSlice:
    """The lines of one definition, with the numbers a reader needs to find it
    again in the file."""

    rel_path: str
    qualified_name: str
    kind: str
    # The definition itself, not counting any lead-in lines.
    line: int
    end_line: int
    # The first line actually returned, which may be earlier than ``line``.
    start_line: int
    text: str = ""
    # Why the code is missing or suspect, "" when it is neither.
    problem: str = ""

    @property
    def available(self) -> bool:
        return not self.problem

    @property
    def location(self) -> str:
        """The answer, in the form a user can act on: a file and a line."""
        return f"{self.rel_path}:{self.line}"

    def numbered(self) -> str:
        """The slice with line numbers, so a model quoting it back cannot
        invent a location for what it quotes."""
        if not self.text:
            return ""
        width = len(str(self.end_line))
        return "\n".join(
            f"{self.start_line + offset:>{width}}| {line}"
            for offset, line in enumerate(self.text.split("\n"))
        )


def _find_symbol(
    analysis: AnalysisResult, rel_path: str, name: str
) -> tuple[Symbol | None, list[Symbol]]:
    """The definition named, and every definition sharing the name.

    Matches a qualified name first: asking for "Login.handle" must never return
    "Session.handle" merely because it was defined earlier in the file.
    """
    found = next((f for f in analysis.files if f.rel_path == rel_path), None)
    if found is None:
        return None, []

    exact = [s for s in found.symbols if s.qualified_name == name]
    if exact:
        return exact[0], exact

    plain = [s for s in found.symbols if s.name == name]
    return (plain[0] if plain else None), plain


def get_source(
    scan_result: ScanResult,
    analysis: AnalysisResult,
    rel_path: str,
    name: str,
    lead: int = LEAD_LINES,
) -> SourceSlice:
    """The source of one named definition in one file.

    ``name`` is either a plain name or a qualified one as the skeleton reports
    it, so a model can pass back exactly what level 2 gave it.
    """
    symbol, matches = _find_symbol(analysis, rel_path, name)

    if symbol is None:
        problem = _no_such_definition(analysis, rel_path, name)
        if not any(f.rel_path == rel_path for f in analysis.files):
            problem = f"{rel_path} is not an analysed file in this project"
        return SourceSlice(
            rel_path=rel_path,
            qualified_name=name,
            kind="",
            line=0,
            end_line=0,
            start_line=0,
            problem=problem,
        )

    result = SourceSlice(
        rel_path=rel_path,
        qualified_name=symbol.qualified_name or symbol.name,
        kind=symbol.kind.value,
        line=symbol.line,
        end_line=symbol.end_line,
        start_line=symbol.line,
    )

    if len(matches) > 1:
        # Say so rather than picking silently: the model asked about one thing
        # and there are several, which it can only resolve if it knows.
        others = ", ".join(str(s.line) for s in matches[1:])
        result.problem = f"{len(matches)} definitions share this name; also at line {others}"

    scanned = next((f for f in scan_result.files if f.rel_path == rel_path), None)
    if scanned is None:
        result.problem = f"{rel_path} is no longer in the scan"
        return result

    try:
        text = read_source(Path(scanned.path))
    except OSError as exc:
        result.problem = f"unreadable: {exc.strerror}"
        return result

    lines = text.split("\n")

    if symbol.end_line > len(lines):
        # The file shrank since the index was built. Anything returned now
        # would be lines that happen to sit at those numbers today.
        result.problem = (
            f"file has changed since it was indexed: it now has {len(lines)} lines, "
            f"and this definition was recorded at {symbol.line}-{symbol.end_line}"
        )
        return result

    start = _attached_start(lines, symbol.line, lead)
    result.start_line = start
    result.text = "\n".join(lines[start - 1 : symbol.end_line])

    if not _looks_like(lines[symbol.line - 1], symbol):
        result.problem = (
            f"file has changed since it was indexed: line {symbol.line} no longer "
            f"defines {result.qualified_name}"
        )

    return result


def _looks_like(line: str, symbol: Symbol) -> bool:
    """Whether the recorded line still defines the symbol it was recorded for.

    A name check, not a parse: the point is to catch a file edited since the
    scan, and for that, finding the name on the line it was indexed at is
    evidence enough. Decorated definitions are the exception worth allowing -
    a decorator line carries the name of the decorator, not the definition.
    """
    stripped = line.strip()
    if stripped.startswith("@"):
        return True
    return symbol.name in stripped


def _attached_start(lines: list[str], definition_line: int, lead: int) -> int:
    """The first line that still belongs to the definition.

    Walks up from the definition through decorators and comments and stops at
    anything else, a blank line included. A fixed number of lines of context
    would have been simpler and wrong: three lines above a definition is
    usually the tail of the previous one, which is code the caller did not ask
    for and might read as part of what it did.
    """
    start = definition_line

    while start > 1 and definition_line - start < lead:
        candidate = lines[start - 2].strip()
        if not candidate or not candidate.startswith(ATTACHED_PREFIXES):
            break
        start -= 1

    return start


SUGGESTION_LIMIT = 3


def _no_such_definition(analysis: AnalysisResult, rel_path: str, name: str) -> str:
    """Say the name is not there, and offer the ones it was probably meant to be.

    The caller has usually just read this file's skeleton and typed a name from
    memory or from a slightly different spelling. Naming the near misses turns
    a dead end into one more request.
    """
    found = next((f for f in analysis.files if f.rel_path == rel_path), None)
    if found is None:
        return f"no definition called {name!r} in {rel_path}"

    # Matched against plain names as well as qualified ones: a caller who
    # mistypes "dealDamage" is nowhere near "CombatService.dealDamage" by any
    # string measure, and the plain name is what they were reaching for. The
    # qualified form is what comes back, because that is what resolves.
    qualified_by_plain: dict[str, str] = {}
    for symbol in found.symbols:
        qualified = symbol.qualified_name or symbol.name
        qualified_by_plain.setdefault(symbol.name, qualified)
        qualified_by_plain.setdefault(qualified, qualified)

    plain = name.rsplit(".", 1)[-1]
    close = difflib.get_close_matches(
        plain, list(qualified_by_plain), n=SUGGESTION_LIMIT, cutoff=0.6
    )

    if not close:
        return f"no definition called {name!r} in {rel_path}"

    suggestions = list(dict.fromkeys(qualified_by_plain[match] for match in close))
    return (
        f"no definition called {name!r} in {rel_path}; did you mean "
        + ", ".join(suggestions[:SUGGESTION_LIMIT])
        + "?"
    )
