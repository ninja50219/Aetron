"""A map of the project, small enough to hand a model before its first step.

The first real model to run the protocol started blind. Its opening message
named the project and its languages and nothing more, so asked "What starts the
program?" it had to guess a word to search for - and searched "main" eleven
times. A person opening an unfamiliar repository looks at the file tree before
anything else, and so does every coding agent that works well: Aider hands its
model a ranked "repo map" of files and the names they define, fitted to a token
budget; Anthropic's own guidance for agents is to give them lightweight
identifiers such as file paths and let them load the content just in time.
This is that map, for this protocol.

What it contains is what a file browser would show: file names, the names of
the definitions in them, and a few facts the index has already derived - where
the program starts, what it depends on, what its documentation is called. It
never contains code. The index itself still never leaves Aetron; the map is a
view of it cut to a budget, and every cut is stated ("+37 more files in
Assets/"), so the model knows there is more and how to ask for it.

The budget is what makes this cheap rather than a second way of sending the
repository. It is spent in rank order - files that start the program, files
the rest of the project imports, files that define the most - so a small
project fits whole and a large one shows its spine. Files are grouped under
their folder, because repeating "myminecraft/Assets/Scripts/" on every line is
where the tokens of a naive listing go.
"""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.symbols import FileSymbols, SymbolKind
from aetron.scanner.scanner import ScanResult

# A common estimate for English and source code, used only to size the map.
# Being wrong by a fifth costs a slightly longer or shorter map, not a failure.
CHARS_PER_TOKEN = 3.5

DEFAULT_BUDGET = 1500

# How many definitions a file lists before "+N". The first few names of a
# file say what it is; the twentieth says nothing the first five did not.
NAMES_PER_FILE = 6

# How many methods a lone class shows. A Unity script is one class whose
# methods - Start, Update - are the whole story; a file of five classes is
# described well enough by their names.
METHODS_PER_CLASS = 5

# File names that mean "start here" in most ecosystems.
ENTRY_STEMS = frozenset(
    {"main", "__main__", "app", "index", "program", "server", "manage", "cli", "run"}
)


@dataclass
class Overview:
    """The map as text, and which files it named."""

    text: str
    # Files named in full. They count as found: the model may ask for their
    # outline without searching first, exactly as if a search had listed them.
    shown: set[str] = field(default_factory=set)
    omitted: int = 0

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def entry_reason(rel_path: str, symbols: FileSymbols | None) -> str:
    """Why this file probably starts something, or "" when nothing says so.

    Each reason is a fact the index holds, not a guess about intent: a main
    guard, a Main method, a Unity component the engine runs, a Roblox script
    the game runs, or a file named the way its ecosystem names entry points.
    """
    path = PurePosixPath(rel_path)
    name = path.name.lower()
    if symbols is not None:
        if symbols.runs_as_script:
            return "script"
        for symbol in symbols.symbols:
            if symbol.kind in (SymbolKind.FUNCTION, SymbolKind.METHOD) and symbol.name == "Main":
                return "Main"
            if symbol.kind == SymbolKind.CLASS and any(
                base.split(".")[-1] in ("MonoBehaviour", "NetworkBehaviour") for base in symbol.bases
            ):
                return "Unity script"
    if name.endswith((".server.lua", ".server.luau")):
        return "server script"
    if name.endswith((".client.lua", ".client.luau")):
        return "client script"
    if path.stem.lower() in ENTRY_STEMS and len(path.parts) <= 3:
        return "entry name"
    return ""


def describe_file(symbols: FileSymbols | None) -> str:
    """The names a file defines, compactly: "class Player (Start, Update)"."""
    if symbols is None:
        return "(no parser: name only)"
    if symbols.parse_error:
        return "(could not be parsed)"

    top = [
        s for s in symbols.symbols
        if s.kind in (SymbolKind.CLASS, SymbolKind.FUNCTION)
        and "." not in (s.qualified_name or s.name)
    ]
    # Public names first. A file is described by what it offers; its private
    # helpers are how, and on this repository they took half the map's
    # budget to say it. They are still counted in the "+N".
    top.sort(key=lambda s: s.name.startswith("_") and not s.name.startswith("__"))
    classes = [s for s in top if s.kind == SymbolKind.CLASS]

    parts = []
    for symbol in top[:NAMES_PER_FILE]:
        if symbol.kind == SymbolKind.CLASS:
            label = f"class {symbol.name}"
            if len(classes) == 1:
                methods = [
                    m.name for m in symbols.symbols
                    if m.kind == SymbolKind.METHOD
                    and (m.qualified_name or "").startswith(symbol.name + ".")
                ]
                if methods:
                    more = len(methods) - METHODS_PER_CLASS
                    shown = ", ".join(methods[:METHODS_PER_CLASS])
                    label += f" ({shown}{f' +{more}' if more > 0 else ''})"
            parts.append(label)
        else:
            parts.append(symbol.name)

    if len(top) > NAMES_PER_FILE:
        parts.append(f"+{len(top) - NAMES_PER_FILE}")
    if not parts:
        variables = [s.name for s in symbols.symbols if s.kind == SymbolKind.VARIABLE]
        if variables:
            parts = variables[:NAMES_PER_FILE]
    return ", ".join(parts) if parts else "(no definitions)"


def rank_files(scan_result: ScanResult, analysis: AnalysisResult) -> list[tuple[str, str]]:
    """Every file, most worth showing first, with its entry reason."""
    by_path = {f.rel_path: f for f in analysis.files}
    has_graph = any(analysis.imports.values())

    ranked = []
    for file_info in scan_result.files:
        symbols = by_path.get(file_info.rel_path)
        reason = entry_reason(file_info.rel_path, symbols)
        dependents = len(analysis.imported_by.get(file_info.rel_path, ()))
        definitions = len(symbols.symbols) if symbols else 0
        depth = file_info.rel_path.count("/")
        ranked.append(
            (
                (
                    # A file that starts the program is the first thing asked for.
                    0 if reason and reason != "entry name" else 1 if reason else 2,
                    # Then what the rest of the project leans on, when the
                    # graph exists at all; a Lua project has none.
                    -dependents if has_graph else 0,
                    -min(definitions, 50),
                    depth,
                    file_info.rel_path,
                ),
                file_info.rel_path,
                reason,
            )
        )
    ranked.sort()
    return [(path, reason) for _, path, reason in ranked]


def _folder_of(rel_path: str) -> str:
    return rel_path.rsplit("/", 1)[0] + "/" if "/" in rel_path else ""


def _file_line(rel_path: str, reason: str, by_path: dict[str, FileSymbols]) -> str:
    indent = "  " if "/" in rel_path else ""
    tag = f" [{reason}]" if reason else ""
    name = rel_path.rsplit("/", 1)[-1]
    return f"{indent}{name}{tag}: {describe_file(by_path.get(rel_path))}"


# Room kept for the "+N more files" line, which is written after the files are
# chosen and so cannot be measured while choosing them.
OMITTED_NOTE_RESERVE = 200


def _fit(
    ranked: list[tuple[str, str]], by_path: dict[str, FileSymbols], limit: int
) -> list[tuple[str, str]]:
    """The longest prefix of ``ranked`` whose rendering fits ``limit`` chars.

    Costed one file at a time - its line, plus its folder's header the first
    time that folder appears - rather than by rendering the whole map again
    for every file tried, which was quadratic in the size of the project.
    """
    chosen: list[tuple[str, str]] = []
    folders: set[str] = set()
    used = 0
    for rel_path, reason in ranked:
        folder = _folder_of(rel_path)
        cost = len(_file_line(rel_path, reason, by_path)) + 1
        if folder and folder not in folders:
            cost += len(folder) + 1
        reserve = OMITTED_NOTE_RESERVE if len(chosen) + 1 < len(ranked) else 0
        if chosen and used + cost + reserve > limit:
            break
        chosen.append((rel_path, reason))
        folders.add(folder)
        used += cost
    return chosen


def _render(
    chosen: list[tuple[str, str]], by_path: dict[str, FileSymbols]
) -> list[str]:
    """Files grouped under their folder, folders in the order of their best file."""
    folders: dict[str, list[tuple[str, str]]] = {}
    for rel_path, reason in chosen:
        folders.setdefault(_folder_of(rel_path), []).append((rel_path, reason))

    lines = []
    for folder, files in folders.items():
        if folder:
            lines.append(folder)
        for rel_path, reason in files:
            lines.append(_file_line(rel_path, reason, by_path))
    return lines


def _omitted_note(omitted: list[str]) -> str:
    counts: dict[str, int] = {}
    for rel_path in omitted:
        parts = rel_path.split("/")
        folder = "/".join(parts[:2]) + "/" if len(parts) > 2 else (parts[0] + "/" if len(parts) > 1 else "(top level)")
        counts[folder] = counts.get(folder, 0) + 1
    biggest = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:4]
    where = ", ".join(f"{count} in {folder}" for folder, count in biggest)
    return f"+{len(omitted)} more files ({where}). FILES <folder> lists a folder."


def build_overview(
    scan_result: ScanResult,
    analysis: AnalysisResult,
    budget: int = DEFAULT_BUDGET,
    notes: str = "",
) -> Overview:
    """The project as a budgeted map, and the set of files it names."""
    by_path = {f.rel_path: f for f in analysis.files}
    ranked = rank_files(scan_result, analysis)

    languages: dict[str, int] = {}
    for file_info in scan_result.files:
        languages[file_info.language] = languages.get(file_info.language, 0) + 1
    language_list = ", ".join(
        f"{name} ({count})" for name, count in sorted(languages.items(), key=lambda i: -i[1])[:5]
    )

    head = [
        f"Project: {scan_result.root.name} - {len(scan_result.files)} files, "
        f"{scan_result.total_lines} lines: {language_list or 'no source files'}"
    ]
    if notes:
        head.append(f"Summary from an earlier look: {notes.strip()}")

    starts = [f"{path} ({reason})" for path, reason in ranked if reason and reason != "entry name"]
    if starts:
        more = f" +{len(starts) - 5}" if len(starts) > 5 else ""
        head.append("Starts at: " + ", ".join(starts[:5]) + more)

    names = list(dict.fromkeys(d.name for d in scan_result.dependencies))
    if names:
        more = f" +{len(names) - 8}" if len(names) > 8 else ""
        head.append("Depends on: " + ", ".join(names[:8]) + more)
    if scan_result.docs:
        head.append("Docs: " + ", ".join(d.rel_path for d in scan_result.docs[:4]))

    tail = []
    if scan_result.skipped:
        tail.append(f"Not indexed: {len(scan_result.skipped)} skipped files. SKIPPED lists them.")

    fixed = sum(len(line) + 1 for line in head + tail) + 60
    chosen = _fit(ranked, by_path, int(budget * CHARS_PER_TOKEN) - fixed)

    omitted = [path for path, _ in ranked[len(chosen):]]
    body = [f"Files ({len(chosen)} of {len(ranked)}, most important first):"]
    body += _render(chosen, by_path)
    if omitted:
        body.append(_omitted_note(omitted))

    return Overview(
        text="\n".join(head + body + tail),
        shown={path for path, _ in chosen},
        omitted=len(omitted),
    )


def list_folder(
    scan_result: ScanResult,
    analysis: AnalysisResult,
    folder: str,
    budget: int = DEFAULT_BUDGET,
) -> Overview:
    """One folder's files, in the same form as the map. What FILES returns."""
    folder = folder.strip().strip("/").replace("\\", "/")
    prefix = folder.lower() + "/" if folder else ""
    by_path = {f.rel_path: f for f in analysis.files}
    inside = [
        (path, reason) for path, reason in rank_files(scan_result, analysis)
        if path.lower().startswith(prefix)
    ]
    if not inside:
        return Overview(text=f"No indexed files are in {folder or 'the project'}/.")

    chosen = _fit(inside, by_path, int(budget * CHARS_PER_TOKEN) - 60)

    lines = [f"{len(inside)} files in {folder or 'the project'}/:"] + _render(chosen, by_path)
    omitted = [path for path, _ in inside[len(chosen):]]
    if omitted:
        lines.append(_omitted_note(omitted))
    return Overview(text="\n".join(lines), shown={p for p, _ in chosen}, omitted=len(omitted))


def list_skipped(scan_result: ScanResult, analysis: AnalysisResult, limit: int = 40) -> str:
    """What the index left out, and why. What SKIPPED returns."""
    lines = []
    for skipped in scan_result.skipped[:limit]:
        lines.append(f"{skipped.rel_path} - {skipped.reason}")
    if len(scan_result.skipped) > limit:
        lines.append(f"+{len(scan_result.skipped) - limit} more skipped files")
    if scan_result.pruned_dirs:
        shown = ", ".join(scan_result.pruned_dirs[:10])
        more = f" +{len(scan_result.pruned_dirs) - 10}" if len(scan_result.pruned_dirs) > 10 else ""
        lines.append(f"Folders not scanned (dependencies, builds, caches): {shown}{more}")
    return "\n".join(lines) if lines else "Nothing was skipped: every source file is indexed."
