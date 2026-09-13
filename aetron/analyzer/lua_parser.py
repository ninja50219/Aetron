"""Extracting symbols from Lua and Luau without a Lua interpreter.

The fourth language, and the first that does not delimit blocks with braces.
Lua closes with the word ``end``, which changes how extent is found but not the
approach: declarations by pattern, extent by counting what opens against what
closes, and nothing claimed that is not there.

Roblox is the reason this exists. A Rojo project is written in Luau, which is
Lua with type annotations, and its files carry the ``.luau`` extension - a
project of several hundred files that Aetron previously reported as empty,
because the extension was unknown and Wally's vendored ``Packages`` filled the
scan with third-party code. Both are fixed in the scanner; this reads what is
left.

Counting ``end`` is not the same as counting braces. Four keywords open a block
and two close one, ``elseif ... then`` opens nothing despite carrying ``then``,
and ``repeat`` is closed by ``until`` rather than by ``end``. Each of those is a
line in ``_depth_change`` and a test.

What this deliberately does not do is resolve ``require``. In Roblox a require
names an instance in a game tree - ``script.Parent.Shared.Config`` - not a path
on disk, and turning one into a file means reading the Rojo project file and
reproducing its mapping. Requires are recorded as written and resolve to
nothing, which is honest; inventing an edge would be worse than having none.
"""

import re

from aetron.scanner.detect import count_lines

from .references import references_excluding_declarations
from .symbols import FileSymbols, ImportRef, Symbol, SymbolKind

LANGUAGE = "lua"

# "function Tbl.name(...)", "function Tbl:name(...)", "function name(...)",
# with Luau's optional generics and return type.
_FUNCTION_RE = re.compile(
    r"^\s*(local\s+)?function\s+([\w.:]+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)"
)

# "local f = function(...)" and Luau's "local f: T = function(...)".
_ASSIGNED_FUNCTION_RE = re.compile(
    r"^\s*(?:local\s+)?([\w.]+)\s*(?::[^=]+)?=\s*function\s*\(([^)]*)\)"
)

# A module table, which in this language is how a file exposes anything.
_TABLE_RE = re.compile(r"^\s*local\s+(\w+)\s*(?::[^=]+)?=\s*\{")

_LOCAL_RE = re.compile(r"^\s*local\s+(\w+)\s*(?::[^=]+)?=(?!\s*(?:function|require))")

# Luau type declarations.
_TYPE_RE = re.compile(r"^\s*(?:export\s+)?type\s+(\w+)\s*(?:<[^>]*>)?\s*=")

_REQUIRE_RE = re.compile(r"""require\s*\(\s*([^)]+?)\s*\)""")

# Keywords that open a block, and the two that close one. "elseif" carries a
# "then" that continues a block rather than opening one, so it is subtracted.
_OPENERS = (r"\bfunction\b", r"\bthen\b", r"\bdo\b", r"\brepeat\b")
_CLOSERS = (r"\bend\b", r"\buntil\b")
_ELSEIF_RE = re.compile(r"\belseif\b")

_KEYWORDS = frozenset(
    """and break do else elseif end false for function goto if in local nil not or
    repeat return then true until while self type export continue""".split()
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")


def parse(text: str, rel_path: str) -> FileSymbols:
    """Parse Lua or Luau source into symbols and requires. Never raises."""
    result = FileSymbols(rel_path=rel_path, language=LANGUAGE)

    name = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    result.symbols.append(
        Symbol(
            name=name,
            kind=SymbolKind.MODULE,
            line=1,
            end_line=count_lines(text) or 1,
            qualified_name=name,
        )
    )

    blanked = _blank_literals(text)
    lines = blanked.split("\n")

    result.imports = _collect_requires(_blank_literals(text, keep_strings=True))
    result.symbols.extend(_collect_symbols(lines))
    result.references = references_excluding_declarations(
        _IDENTIFIER_RE.findall(blanked), result, _KEYWORDS
    )
    result.dynamic_prefixes = _collect_dynamic_prefixes(text)
    return result


def _blank_literals(text: str, keep_strings: bool = False) -> str:
    """Replace comments and strings with spaces, keeping every line number.

    Lua's long brackets - ``[[ ]]``, ``[==[ ]==]`` - serve as both strings and
    comments and may run over many lines, so they are matched by their own
    level rather than by a fixed pattern.
    """
    out = []
    index = 0
    length = len(text)

    while index < length:
        long_bracket = _long_bracket_at(text, index)
        comment = text.startswith("--", index)

        if comment and long_bracket is None:
            nested = _long_bracket_at(text, index + 2)
            if nested is not None:
                end = _end_of_long_bracket(text, nested, index + 2)
                out.append(_spaces(text[index:end]))
                index = end
                continue
            end = text.find("\n", index)
            end = length if end == -1 else end
            out.append(" " * (end - index))
            index = end

        elif long_bracket is not None:
            end = _end_of_long_bracket(text, long_bracket, index)
            body = text[index:end]
            out.append(body if keep_strings else _spaces(body))
            index = end

        elif text[index] in "\"'":
            end = _end_of_quoted(text, index)
            body = text[index:end]
            out.append(body if keep_strings else _spaces(body))
            index = end

        else:
            out.append(text[index])
            index += 1

    return "".join(out)


def _spaces(body: str) -> str:
    return "".join(c if c == "\n" else " " for c in body)


def _long_bracket_at(text: str, index: int) -> int | None:
    """The level of a long bracket starting here, or None. "[[" is level 0."""
    if index >= len(text) or text[index] != "[":
        return None

    level = 0
    probe = index + 1
    while probe < len(text) and text[probe] == "=":
        level += 1
        probe += 1

    return level if probe < len(text) and text[probe] == "[" else None


def _end_of_long_bracket(text: str, level: int, start: int) -> int:
    closing = "]" + "=" * level + "]"
    end = text.find(closing, start)
    return len(text) if end == -1 else end + len(closing)


def _end_of_quoted(text: str, start: int) -> int:
    quote = text[start]
    index = start + 1

    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index + 1
        if char == "\n":
            return index  # unterminated: ends at the line, not the file
        index += 1

    return index


def _depth_change(line: str) -> int:
    """How many blocks this line opens, minus how many it closes.

    "elseif ... then" carries a "then" but continues the block it is already
    in rather than opening another, so it cancels its own keyword.
    """
    opened = sum(len(re.findall(pattern, line)) for pattern in _OPENERS)
    opened -= len(_ELSEIF_RE.findall(line))
    closed = sum(len(re.findall(pattern, line)) for pattern in _CLOSERS)
    return opened - closed


def _end_of_block(lines: list[str], start: int) -> int:
    """The line a declaration beginning at ``start`` ends on."""
    depth = 0

    for number in range(start, len(lines) + 1):
        depth += _depth_change(lines[number - 1])
        if depth <= 0:
            return number

    return start


def _collect_symbols(lines: list[str]) -> list[Symbol]:
    """Every declaration, with the ones inside a function left out.

    Only the top level is reported. A local inside a function body is not
    something another file can reach, and listing it would fill a skeleton
    with names nothing can call.
    """
    symbols: list[Symbol] = []
    depth = 0

    for number, line in enumerate(lines, start=1):
        if depth == 0:
            symbol = _declaration(lines, number, line)
            if symbol is not None:
                symbols.append(symbol)

        depth = max(depth + _depth_change(line), 0)

    return symbols


def _declaration(lines: list[str], number: int, line: str) -> Symbol | None:
    function = _FUNCTION_RE.match(line)
    if function:
        raw = function.group(2)
        # "Tbl:name" is a method with an implicit self; "Tbl.name" is a field
        # holding a function. Both read as a method to anyone looking for one.
        method = ":" in raw
        qualified = raw.replace(":", ".")
        name = qualified.rsplit(".", 1)[-1]
        parameters = _parameters(function.group(3))
        if method:
            parameters = ["self", *parameters]

        return Symbol(
            name=name,
            kind=SymbolKind.METHOD if "." in qualified else SymbolKind.FUNCTION,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=qualified,
            parameters=parameters,
        )

    assigned = _ASSIGNED_FUNCTION_RE.match(line)
    if assigned and assigned.group(1) not in _KEYWORDS:
        return Symbol(
            name=assigned.group(1).rsplit(".", 1)[-1],
            kind=SymbolKind.FUNCTION,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=assigned.group(1),
            parameters=_parameters(assigned.group(2)),
        )

    type_decl = _TYPE_RE.match(line)
    if type_decl:
        return Symbol(
            name=type_decl.group(1),
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=number,
            qualified_name=type_decl.group(1),
        )

    table = _TABLE_RE.match(line)
    if table and table.group(1) not in _KEYWORDS:
        return Symbol(
            name=table.group(1),
            kind=SymbolKind.CLASS,
            line=number,
            end_line=_end_of_block(lines, number) if "}" not in line else number,
            qualified_name=table.group(1),
        )

    local = _LOCAL_RE.match(line)
    if local and local.group(1) not in _KEYWORDS:
        return Symbol(
            name=local.group(1),
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=number,
            qualified_name=local.group(1),
        )

    return None


def _parameters(raw: str) -> list[str]:
    """Parameter names, with Luau's type annotations and defaults removed."""
    found = []
    for part in raw.split(","):
        part = part.split("=")[0].split(":")[0].strip()
        if not part:
            continue
        if part.startswith("..."):
            found.append("...")
            continue
        names = _IDENTIFIER_RE.findall(part)
        if names:
            found.append(names[0])
    return found


def _collect_requires(text: str) -> list[ImportRef]:
    """Every require, as written.

    A Roblox require names an instance in a game tree rather than a path on
    disk, so these are recorded and never resolved. Saying where a require
    points would mean reproducing Rojo's mapping from its project file.
    """
    found = []
    for number, line in enumerate(text.split("\n"), start=1):
        for match in _REQUIRE_RE.finditer(line):
            target = match.group(1).strip().strip("\"'")
            if target:
                found.append(ImportRef(module=target, line=number))
    return found


def _collect_dynamic_prefixes(text: str) -> set[str]:
    """String fragments joined to something else, which is how Lua writes
    dynamic dispatch: obj["on" .. name]."""
    prefixes = set()
    for match in re.finditer(r"""['"]([^'"\n]{3,})['"]\s*\.\.""", text):
        prefixes.add(match.group(1))
    return prefixes
