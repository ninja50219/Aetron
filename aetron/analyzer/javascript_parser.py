"""Extracting symbols from JavaScript and TypeScript without a JS engine.

The third language, and the one with the most ways to say the same thing. A
function in this family may be written as a declaration, an expression assigned
to a name, an arrow assigned to a name, a class method, a getter, a static
member or a field holding an arrow - and all of those are the same thing to
someone reading the file looking for where something happens.

So this recognises the forms people actually write, by pattern, with extent
found by counting braces. It shares the C# parser's discipline and its limits:
not a compiler, conservative by design, and built to fail by leaving something
out rather than by claiming something that is not there. A missed function
costs a search result; an invented one sends a reader to a line that means
something else.

TypeScript is handled in the same file rather than a separate one, because the
declarations are the same declarations with types attached, and a second
parser would be the first one plus four patterns and a copy of every bug.

Strings, comments, template literals and regular expression literals are
blanked before anything counts a brace. Template literals are blanked whole,
including any ``${...}`` inside them: the expressions there are real code, and
losing a few references is a much smaller price than miscounting every brace
in the file after one.
"""

import re
from dataclasses import dataclass

from .references import references_excluding_declarations
from .symbols import FileSymbols, ImportRef, Symbol, SymbolKind

LANGUAGE = "javascript"
# TypeScript is the same grammar with types, so one parser serves both. The
# scanner still reports them as different languages, which is what a reader
# wants to see.
TYPESCRIPT = "typescript"

_EXPORT = r"(?:export\s+(?:default\s+)?)?"
_ASYNC = r"(?:async\s+)?"

_FUNCTION_RE = re.compile(
    r"^\s*" + _EXPORT + _ASYNC + r"function\s*(\*)?\s*(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)"
)

# A function or arrow bound to a name. The name is what anyone searches for,
# so these matter as much as declarations.
_ASSIGNED_FUNCTION_RE = re.compile(
    r"^\s*" + _EXPORT + r"(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*"
    r"(?:" + _ASYNC + r"function\s*\*?\s*\(([^)]*)\)"
    r"|" + _ASYNC + r"(?:\(([^)]*)\)|(\w+))\s*(?::[^=]*?)?=>)"
)

_CLASS_RE = re.compile(
    r"^\s*" + _EXPORT + r"(?:abstract\s+)?class\s+(\w+)\s*(?:<[^>]*>)?\s*"
    r"(?:extends\s+([\w.<>\[\]]+)\s*)?(?:implements\s+([^{]+))?"
)

_INTERFACE_RE = re.compile(
    r"^\s*" + _EXPORT + r"interface\s+(\w+)\s*(?:<[^>]*>)?\s*"
    r"(?:extends\s+([^{]+))?"
)

_TYPE_ALIAS_RE = re.compile(r"^\s*" + _EXPORT + r"type\s+(\w+)\s*(?:<[^>]*>)?\s*=")
_ENUM_RE = re.compile(r"^\s*" + _EXPORT + r"(?:const\s+)?enum\s+(\w+)")
_NAMESPACE_RE = re.compile(r"^\s*" + _EXPORT + r"(?:namespace|module)\s+([\w.]+)\s*\{")

# A plain top-level binding that is not a function: a constant, a config table.
_CONSTANT_RE = re.compile(
    r"^\s*" + _EXPORT + r"(?:const|let|var)\s+(\w+)\s*(?::[^=]+)?=\s*(?!.*=>)"
)

# Class members. Modifiers are TypeScript's; JavaScript has static and #private.
_MEMBER_MODIFIERS = (
    r"(?:(?:public|private|protected|readonly|static|abstract|override|declare)\s+)*"
)

_METHOD_RE = re.compile(
    r"^\s*" + _MEMBER_MODIFIERS + _ASYNC + r"(?:(get|set)\s+)?(\*)?\s*"
    r"(#?\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?::[^{;]+)?\s*(?:\{|;|$)"
)

_FIELD_RE = re.compile(
    r"^\s*" + _MEMBER_MODIFIERS + r"(#?\w+)\s*(?:[?!])?\s*(?::[^=;]+)?\s*(?:=[^;]*)?;?\s*$"
)

_IMPORT_FROM_RE = re.compile(
    r"""^\s*(?:import|export)\s+(?:type\s+)?(.*?)\s*from\s*['"]([^'"]+)['"]"""
)
_IMPORT_BARE_RE = re.compile(r"""^\s*import\s*['"]([^'"]+)['"]""")
_EXPORT_ALL_RE = re.compile(r"""^\s*export\s+\*\s*(?:as\s+\w+\s+)?from\s*['"]([^'"]+)['"]""")
_REQUIRE_RE = re.compile(r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")

_KEYWORDS = frozenset(
    """await break case catch class const continue debugger default delete do else
    enum export extends false finally for function if implements import in instanceof
    interface let new null return static super switch this throw true try typeof var
    void while with yield as from of get set async declare namespace module type
    readonly public private protected abstract override satisfies keyof infer is""".split()
)

_IDENTIFIER_RE = re.compile(r"[A-Za-z_$][\w$]*")

# Characters after which a "/" starts a regular expression rather than being
# division. Getting this wrong the other way - treating a division as a regex -
# would blank the rest of the line.
_REGEX_PRECEDERS = set("(,=:[!&|?{};+-*%~^<>")


@dataclass
class _Scope:
    depth: int
    name: str
    is_type: bool
    entered: bool = False


def parse(text: str, rel_path: str) -> FileSymbols:
    """Parse JavaScript or TypeScript into symbols and imports.

    Never raises. The language is reported from the file's extension, so a
    .ts file says typescript even though one parser reads both.
    """
    language = TYPESCRIPT if rel_path.rsplit(".", 1)[-1] in ("ts", "tsx") else LANGUAGE
    result = FileSymbols(rel_path=rel_path, language=language)

    name = rel_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    result.symbols.append(
        Symbol(
            name=name,
            kind=SymbolKind.MODULE,
            line=1,
            end_line=len(text.splitlines()) or 1,
            qualified_name=name,
        )
    )

    blanked = _blank_literals(text)
    lines = blanked.split("\n")

    # Imports are read from a version that keeps its strings: the module being
    # imported is a string literal, so the blanked text names nothing.
    result.imports = _collect_imports(_blank_literals(text, keep_strings=True).split("\n"))
    result.symbols.extend(_collect_symbols(lines))
    result.references = references_excluding_declarations(
        _IDENTIFIER_RE.findall(blanked), result, _KEYWORDS
    )
    result.dynamic_prefixes = _collect_dynamic_prefixes(text)
    return result


def _blank_literals(text: str, keep_strings: bool = False) -> str:
    """Replace strings, comments, template literals and regexes with spaces.

    Line structure is preserved exactly, so every line number stays true.

    ``keep_strings`` writes ordinary string literals back unchanged while still
    consuming them, which is what the import pass needs: in this language the
    thing being imported *is* a string, and blanking it left every import
    pointing at nothing. Comments are still removed, so a commented-out import
    is not mistaken for a real one.
    """
    out = []
    index = 0
    length = len(text)
    last_significant = ""

    while index < length:
        char = text[index]
        two = text[index : index + 2]

        if two == "//":
            end = text.find("\n", index)
            end = length if end == -1 else end
            out.append(" " * (end - index))
            index = end

        elif two == "/*":
            end = text.find("*/", index + 2)
            end = length if end == -1 else end + 2
            out.append(_spaces(text[index:end]))
            index = end

        elif char in "\"'":
            end, blanked = _skip_quoted(text, index)
            out.append(text[index:end] if keep_strings else blanked)
            index = end
            last_significant = "x"

        elif char == "`":
            index, blanked = _skip_template(text, index)
            out.append(blanked)
            last_significant = "x"

        elif char == "/" and last_significant in _REGEX_PRECEDERS:
            index, blanked = _skip_regex(text, index)
            out.append(blanked)
            last_significant = "x"

        else:
            out.append(char)
            if not char.isspace():
                last_significant = char
            index += 1

    return "".join(out)


def _spaces(body: str) -> str:
    return "".join(c if c == "\n" else " " for c in body)


def _skip_quoted(text: str, start: int) -> tuple[int, str]:
    quote = text[start]
    index = start + 1

    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            index += 1
            break
        if char == "\n":
            break  # an unterminated string ends at the line, not the file
        index += 1

    return index, _spaces(text[start:index])


def _skip_template(text: str, start: int) -> tuple[int, str]:
    """Consume a template literal whole, interpolations included.

    The expressions inside ``${...}`` are real code, and reading them would
    find a few more references. It would also mean tracking brace depth inside
    a string, and one mistake there miscounts every brace in the rest of the
    file. The references are worth less than the line numbers.
    """
    index = start + 1
    depth = 0

    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if text[index : index + 2] == "${":
            depth += 1
            index += 2
            continue
        if char == "}" and depth:
            depth -= 1
        elif char == "`" and not depth:
            index += 1
            break
        index += 1

    return index, _spaces(text[start:index])


def _skip_regex(text: str, start: int) -> tuple[int, str]:
    index = start + 1
    in_class = False

    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            index += 1
            while index < len(text) and text[index].isalpha():
                index += 1  # flags
            break
        elif char == "\n":
            # Not a regex after all; a lone "/" on a line is division.
            return start + 1, "/"
        index += 1

    return index, _spaces(text[start:index])


def _collect_imports(lines: list[str]) -> list[ImportRef]:
    found = []

    for number, line in enumerate(lines, start=1):
        star = _EXPORT_ALL_RE.match(line)
        if star:
            found.append(ImportRef(module=star.group(1), line=number))
            continue

        from_import = _IMPORT_FROM_RE.match(line)
        if from_import:
            found.append(
                ImportRef(
                    module=from_import.group(2),
                    names=_imported_names(from_import.group(1)),
                    line=number,
                    from_import=True,
                )
            )
            continue

        bare = _IMPORT_BARE_RE.match(line)
        if bare:
            found.append(ImportRef(module=bare.group(1), line=number))
            continue

        for match in _REQUIRE_RE.finditer(line):
            found.append(ImportRef(module=match.group(1), line=number))

        for match in _DYNAMIC_IMPORT_RE.finditer(line):
            found.append(ImportRef(module=match.group(1), line=number))

    return found


def _imported_names(clause: str) -> list[str]:
    """The names an import clause binds, ignoring how they were renamed."""
    names = []
    for part in re.split(r"[,{}]", clause):
        part = part.strip()
        if not part or part.startswith("*"):
            continue
        # "a as b" binds b, but a is the name in the other file.
        names.append(part.split()[0])
    return [name for name in names if _IDENTIFIER_RE.fullmatch(name)]


def _collect_symbols(lines: list[str]) -> list[Symbol]:
    symbols: list[Symbol] = []
    scopes: list[_Scope] = []
    depth = 0

    for number, line in enumerate(lines, start=1):
        while scopes and scopes[-1].entered and depth <= scopes[-1].depth:
            scopes.pop()

        enclosing = next((s for s in reversed(scopes) if s.is_type), None)
        prefix = enclosing.name if enclosing else ""
        in_type_body = enclosing is not None and depth == enclosing.depth + 1

        # The body of the innermost scope, or the file itself. Anything deeper
        # is inside a function, where a nested helper is a local and not the
        # file's to offer.
        body_depth = scopes[-1].depth + 1 if scopes else 0
        at_top_level = enclosing is None and depth == body_depth

        symbol = None

        if in_type_body:
            symbol = _member(lines, number, line, prefix)
        elif at_top_level:
            symbol, scope = _declaration(lines, number, line, prefix)
            # A declaration whose block opens and closes on the same line -
            # "class Empty { }", "enum Status { Ok }" - has no body for
            # anything to be inside. Pushing a scope for it left one that
            # could never be entered and so could never be popped, and every
            # later declaration in the file was nested inside it. A line with
            # no brace at all still opens a scope: the brace is on the next
            # line, which is how much of this language is written.
            opens_a_body = "{" not in line or line.count("{") > line.count("}")
            if scope is not None and opens_a_body:
                scopes.append(_Scope(depth, scope[0], is_type=scope[1]))

        if symbol is not None:
            symbols.append(symbol)

        depth += line.count("{") - line.count("}")

        for scope in scopes:
            if depth > scope.depth:
                scope.entered = True

    return symbols


def _declaration(
    lines: list[str], number: int, line: str, prefix: str
) -> tuple[Symbol | None, tuple[str, bool] | None]:
    """A top-level declaration, and the scope it opens if it opens one."""
    namespace = _NAMESPACE_RE.match(line)
    if namespace:
        return None, (namespace.group(1), False)

    klass = _CLASS_RE.match(line)
    if klass:
        bases = [b for b in (klass.group(2),) if b]
        bases.extend(_split_list(klass.group(3)))
        symbol = Symbol(
            name=klass.group(1),
            kind=SymbolKind.CLASS,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=klass.group(1),
            bases=bases,
        )
        return symbol, (klass.group(1), True)

    interface = _INTERFACE_RE.match(line)
    if interface:
        symbol = Symbol(
            name=interface.group(1),
            kind=SymbolKind.CLASS,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=interface.group(1),
            bases=_split_list(interface.group(2)),
        )
        return symbol, (interface.group(1), True)

    enum = _ENUM_RE.match(line)
    if enum:
        symbol = Symbol(
            name=enum.group(1),
            kind=SymbolKind.CLASS,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=enum.group(1),
        )
        return symbol, (enum.group(1), False)

    function = _FUNCTION_RE.match(line)
    if function and function.group(2) not in _KEYWORDS:
        return (
            Symbol(
                name=function.group(2),
                kind=SymbolKind.FUNCTION,
                line=number,
                end_line=_end_of_block(lines, number),
                qualified_name=function.group(2),
                parameters=_parameters(function.group(3)),
            ),
            None,
        )

    assigned = _ASSIGNED_FUNCTION_RE.match(line)
    if assigned and assigned.group(1) not in _KEYWORDS:
        raw = next((g for g in assigned.groups()[1:] if g is not None), "")
        return (
            Symbol(
                name=assigned.group(1),
                kind=SymbolKind.FUNCTION,
                line=number,
                end_line=_end_of_block(lines, number),
                qualified_name=assigned.group(1),
                parameters=_parameters(raw),
            ),
            None,
        )

    alias = _TYPE_ALIAS_RE.match(line)
    if alias:
        return (
            Symbol(
                name=alias.group(1),
                kind=SymbolKind.VARIABLE,
                line=number,
                end_line=_end_of_block(lines, number),
                qualified_name=alias.group(1),
            ),
            None,
        )

    constant = _CONSTANT_RE.match(line)
    if constant and constant.group(1) not in _KEYWORDS:
        return (
            Symbol(
                name=constant.group(1),
                kind=SymbolKind.VARIABLE,
                line=number,
                end_line=_end_of_block(lines, number),
                qualified_name=constant.group(1),
            ),
            None,
        )

    return None, None


def _member(lines: list[str], number: int, line: str, prefix: str) -> Symbol | None:
    method = _METHOD_RE.match(line)
    if method and method.group(3) not in _KEYWORDS:
        name = method.group(3)
        return Symbol(
            name=name,
            kind=SymbolKind.METHOD,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.{name}" if prefix else name,
            parameters=_parameters(method.group(4)),
        )

    field = _FIELD_RE.match(line)
    if field and field.group(1) not in _KEYWORDS:
        return Symbol(
            name=field.group(1),
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=number,
            qualified_name=f"{prefix}.{field.group(1)}" if prefix else field.group(1),
        )

    return None


def _split_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parameters(raw: str) -> list[str]:
    """Parameter names, from a list that may hold types, defaults and patterns.

    A destructured parameter has no name of its own, so the names inside it are
    reported instead - which is what someone reading the signature sees.
    """
    if not raw:
        return []

    names = []
    depth = 0
    current = ""

    for char in raw:
        if char in "<([{":
            depth += 1
        elif char in ">)]}":
            depth -= 1
        if char == "," and depth == 0:
            names.append(current)
            current = ""
        else:
            current += char
    names.append(current)

    found = []
    for part in names:
        part = part.split("=")[0].split(":")[0].strip()
        part = part.lstrip(".")  # a rest parameter
        if not part:
            continue
        if part.startswith(("{", "[")):
            found.extend(
                word
                for word in _IDENTIFIER_RE.findall(part)
                if word not in _KEYWORDS
            )
            continue
        words = _IDENTIFIER_RE.findall(part)
        if words:
            found.append(words[0])

    return found


def _end_of_block(lines: list[str], start: int) -> int:
    """Where a declaration beginning at ``start`` ends.

    Braces inside parentheses or brackets do not open a block. A destructured
    parameter - "handleLogin({ username, password })" - is an ordinary
    signature in this language, and counting its braces ended every such
    method on its own first line.
    """
    depth = 0
    nesting = 0
    opened = False

    for number in range(start, len(lines) + 1):
        line = lines[number - 1]

        for char in line:
            if char in "([":
                nesting += 1
            elif char in ")]":
                nesting -= 1
            elif char == "{" and nesting <= 0:
                depth += 1
                opened = True
            elif char == "}" and nesting <= 0:
                depth -= 1
                if opened and depth == 0:
                    return number

        if not opened and (";" in line or (number > start and line.strip())):
            # A one-line arrow or a type alias with no block of its own.
            return number

    return start


def _collect_dynamic_prefixes(text: str) -> set[str]:
    """String fragments joined to something else at runtime.

    The JavaScript equivalents of getattr are obj[name] and obj["on" + name],
    and template literals are the common spelling of the second.
    """
    prefixes = set()
    for match in re.finditer(r"""['"]([^'"\n]{3,})['"]\s*\+""", text):
        prefixes.add(match.group(1))
    for match in re.finditer(r"`([^`{\n]{3,})\$\{", text):
        prefixes.add(match.group(1))
    return prefixes
