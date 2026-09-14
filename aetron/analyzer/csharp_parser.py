"""Extracting symbols from C# source without a C# compiler.

C# is the second language Aetron reads, and the first one with no parser in the
standard library. Rather than take a dependency, this reads the shape of the
file: declarations are found by pattern and their extent by counting braces.

That is a real limit and worth stating plainly. This is not a compiler and
cannot be: it does not resolve types, does not expand generics, and will not
understand a construct written in a way the patterns here do not anticipate.
What it does do is find the declarations a reader would find by scrolling, with
the line numbers that let the level above ask for one of them. For a protocol
whose job is to point at code rather than reason about it, that is the whole
requirement.

The one thing it must never do is claim a definition that is not there. A
missed method costs a search result; an invented one sends a model to a line
that means something else, and every level above trusts these line numbers.
So the patterns are deliberately conservative, and anything ambiguous is left
out rather than guessed at.

Strings and comments are blanked before anything else runs. A brace inside a
string literal is not a scope, and a file that ends mid-comment would otherwise
swallow the rest of the project's understanding of it.
"""

import re
from dataclasses import dataclass

from aetron.scanner.detect import count_lines

from .references import references_excluding_declarations
from .symbols import FileSymbols, ImportRef, Symbol, SymbolKind

LANGUAGE = "csharp"

# Modifiers that may precede a declaration, in any order and any number.
_MODIFIERS = (
    r"(?:(?:public|private|protected|internal|static|abstract|virtual|override|"
    r"sealed|partial|readonly|const|async|extern|unsafe|new|required|file)\s+)*"
)

# A type: a name, optionally generic, optionally an array, optionally nullable.
# The "?" belongs inside the class as well as at the end: it appears on type
# arguments too, and "Task<TEntity?>" is an ordinary signature in any codebase
# with nullable reference types turned on.
_TYPE = r"(?:[\w.<>,\[\]\s?]*?[\w>\]?])"

_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([\w.]+)\s*[{;]?\s*$")

_USING_RE = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?([\w.]+)\s*;")
_USING_ALIAS_RE = re.compile(r"^\s*using\s+(\w+)\s*=\s*([\w.<>,\[\]]+)\s*;")

# The optional parameter list is a primary constructor: every positional
# record has one, and since C# 12 a plain class may too. Without it a
# positional record - "record Person(string Name);" - was not a type at all.
_TYPE_DECL_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(class|struct|interface|record|enum)\s+"
    r"(?:class\s+)?"
    r"(\w+)\s*(?:<[^>]*>)?\s*(\([^)]*\))?\s*(?::\s*([^{;]+?))?"
    r"(?:\s*where\s[^{;]*)?\s*(?:\{|;|$)"
)

# A method: a return type, a name, a parameter list. Constructors have no
# return type, so they are matched separately rather than by making the type
# optional - an optional type turns every field declaration into a method.
_METHOD_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(?!(?:class|struct|interface|record|enum|return|new|if|"
    r"for|foreach|while|switch|using|lock|catch)\b)"
    r"(" + _TYPE + r")\s+(\w+)\s*(?:<[^>]*>)?\s*\(([^)]*)\)\s*(?:where[^{;]*)?\s*(?:\{|=>|;|$)"
)

_CONSTRUCTOR_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(\w+)\s*\(([^)]*)\)\s*(?::\s*(?:base|this)\s*\([^)]*\))?\s*(?:\{|$)"
)

# A property has a body of accessors rather than a parameter list.
_PROPERTY_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(" + _TYPE + r")\s+(\w+)\s*(?:\{\s*(?:get|set|init)|=>)"
)

# A property whose accessor block opens on the following line. Matched only
# when that line really does open a block, because a bare "Type Name" line is
# otherwise indistinguishable from the middle of a wrapped expression.
_PROPERTY_NEXT_LINE_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(" + _TYPE + r")\s+(\w+)\s*$"
)

_FIELD_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(" + _TYPE + r")\s+(\w+)\s*(?:=[^;]*)?;\s*$"
)

# Words that are never a definition's name, so a line matching a pattern with
# one of these as its "name" is a statement that happens to look like one.
_KEYWORDS = frozenset(
    """abstract as base bool break byte case catch char checked class const continue
    decimal default delegate do double else enum event explicit extern false finally
    fixed float for foreach goto if implicit in int interface internal is lock long
    namespace new null object operator out override params private protected public
    readonly ref return sbyte sealed short sizeof stackalloc static string struct
    switch this throw true try typeof uint ulong unchecked unsafe ushort using
    virtual void volatile while var record init get set value when where yield
    async await nameof""".split()
)

# An operator overload and an indexer are declarations whose "name" is not an
# identifier, so the ordinary member patterns cannot reach them.
_OPERATOR_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(?:" + _TYPE + r")\s+operator\s*"
    r"(\S{1,3}?)\s*\(([^)]*)\)"
)

_INDEXER_RE = re.compile(
    r"^\s*" + _MODIFIERS + r"(?:" + _TYPE + r")\s+this\s*\[([^\]]*)\]"
)

# How many lines a declaration may be spread over before it is given up on.
# A wrapped parameter list is ordinary in any codebase with a line limit, and
# a signature that only matched when it fitted on one line missed them all.
MAX_JOINED_LINES = 10

_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")


@dataclass
class _Scope:
    """A namespace or type the parser is currently inside."""

    depth: int
    name: str
    is_type: bool
    # False until a brace has actually opened this scope. Without it, a
    # declaration whose brace is on the next line closes immediately.
    entered: bool = False


def parse(text: str, rel_path: str) -> FileSymbols:
    """Parse C# source into symbols and using directives.

    Never raises: a file this cannot make sense of comes back with whatever was
    recognised, which is the same contract the Python parser offers.
    """
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

    result.imports = _collect_usings(lines)
    result.symbols.extend(_collect_symbols(lines))
    result.references = references_excluding_declarations(
        _IDENTIFIER_RE.findall(blanked), result, _KEYWORDS
    )
    result.dynamic_prefixes = _collect_dynamic_prefixes(text)
    return result


def _blank_literals(text: str) -> str:
    """Replace the contents of strings and comments with spaces.

    Line structure is preserved exactly, so every line number stays true. This
    has to run before anything counts a brace: a brace in a string is not a
    scope, and "// }" would close one that was never opened.
    """
    out = []
    index = 0
    length = len(text)

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
            out.append("".join(c if c == "\n" else " " for c in text[index:end]))
            index = end

        elif char == '"' or char == "'":
            index, blanked = _skip_string(text, index)
            out.append(blanked)

        else:
            out.append(char)
            index += 1

    return "".join(out)


def _skip_string(text: str, start: int) -> tuple[int, str]:
    """Consume one string or character literal, returning its blanked form."""
    quote = text[start]
    verbatim = start > 0 and text[start - 1] == "@"
    raw = text.startswith('"""', start)

    if raw:
        end = text.find('"""', start + 3)
        end = len(text) if end == -1 else end + 3
    else:
        index = start + 1
        while index < len(text):
            char = text[index]
            if char == "\\" and not verbatim:
                index += 2
                continue
            if char == quote:
                if verbatim and text[index : index + 2] == quote * 2:
                    index += 2
                    continue
                index += 1
                break
            if char == "\n" and not verbatim:
                break  # an unterminated literal ends at the line, not the file
            index += 1
        end = index

    body = text[start:end]
    return end, "".join(c if c == "\n" else " " for c in body)


def _collect_usings(lines: list[str]) -> list[ImportRef]:
    found = []

    for number, line in enumerate(lines, start=1):
        alias = _USING_ALIAS_RE.match(line)
        if alias:
            found.append(
                ImportRef(module=alias.group(2), names=[alias.group(1)], line=number)
            )
            continue

        match = _USING_RE.match(line)
        if match and "=" not in line:
            found.append(ImportRef(module=match.group(1), line=number))

    return found


def _end_of_block(lines: list[str], start: int) -> int:
    """The line a declaration beginning at ``start`` ends on.

    Counts braces from the declaration onward. A declaration with no body - an
    interface method, an abstract method, a field - ends at its semicolon,
    which is the first thing checked.
    """
    depth = 0
    opened = False

    for number in range(start, len(lines) + 1):
        line = lines[number - 1]

        for char in line:
            if char == "{":
                depth += 1
                opened = True
            elif char == "}":
                depth -= 1
                if opened and depth == 0:
                    return number

        if not opened and ";" in line:
            return number

    # Unbalanced braces: the file is truncated or this parser misread it.
    # Claiming the rest of the file would be worse than claiming one line.
    return start


def _parameters(raw: str) -> list[str]:
    """Parameter names from a C# parameter list.

    The name is the last word of each parameter, after its type, its modifiers
    and any default value.
    """
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
        part = part.split("=")[0].strip()
        if not part:
            continue
        words = _IDENTIFIER_RE.findall(part)
        if words:
            found.append(words[-1])

    return found


def _collect_symbols(lines: list[str]) -> list[Symbol]:
    """Every declaration in the file, with the scope it sits in.

    Scope is tracked by brace depth rather than by indentation, because C# is
    not indentation-sensitive and plenty of real code is not indented the way
    its author intended.

    A scope has to record whether it has been entered yet, not just the depth
    it was declared at. C# is commonly written with the opening brace on the
    line after the declaration, which means a class is declared at the same
    depth it will be closed at, and a scope that popped on equal depth was
    discarded one line after it opened - taking every member of every
    Allman-braced class with it.
    """
    symbols: list[Symbol] = []
    scopes: list[_Scope] = []
    depth = 0

    for number, line in enumerate(lines, start=1):
        while scopes and scopes[-1].entered and depth <= scopes[-1].depth:
            scopes.pop()

        enclosing = next((s for s in reversed(scopes) if s.is_type), None)
        prefix = enclosing.name if enclosing else ""

        # A member sits directly inside its type's braces. Anything deeper is
        # inside a method body, where "var message = ..." is a local and
        # "return View();" is a call - both of which match the declaration
        # patterns and neither of which is a declaration. Claiming them would
        # send a reader to a line that means something else.
        in_type_body = enclosing is not None and depth == enclosing.depth + 1

        symbol = None

        namespace = _NAMESPACE_RE.match(line)
        type_decl = _TYPE_DECL_RE.match(line)

        # A declaration whose block opens and closes on the same line -
        # "class Empty { }" - has no body for anything to be inside. Pushing
        # a scope for it left one that could never be entered and so could
        # never be popped, and every later declaration in the file was nested
        # inside it, corrupting its qualified name. A line with no brace at
        # all still opens a scope: the brace is on the next line, which is how
        # much of this language is written.
        opens_a_body = "{" not in line or line.count("{") > line.count("}")

        if namespace:
            # Namespaces qualify nothing here: a C# namespace is not the file,
            # and prefixing every class with it would make every qualified name
            # differ from what a reader would type.
            if opens_a_body or line.rstrip().endswith(";"):
                # A file-scoped namespace ends in a semicolon and governs
                # the rest of the file, so it opens a body of a kind.
                scopes.append(_Scope(depth, namespace.group(1), is_type=False))

        elif type_decl:
            name, primary, bases = (
                type_decl.group(2),
                type_decl.group(3),
                type_decl.group(4),
            )
            symbol = Symbol(
                name=name,
                kind=SymbolKind.CLASS,
                line=number,
                end_line=_end_of_block(lines, number),
                qualified_name=f"{prefix}.{name}" if prefix else name,
                bases=_bases(bases),
                parameters=_parameters(primary[1:-1]) if primary else [],
            )
            if opens_a_body:
                scopes.append(_Scope(depth, symbol.qualified_name, is_type=True))

        elif in_type_body:
            symbol = _member(_logical_line(lines, number), number, lines, prefix)

        if symbol is not None:
            symbols.append(symbol)

        depth += line.count("{") - line.count("}")

        for scope in scopes:
            if depth > scope.depth:
                scope.entered = True

    return symbols


def _bases(raw: str | None) -> list[str]:
    """Base types and interfaces, as written."""
    if not raw:
        return []

    found = []
    depth = 0
    current = ""

    for char in raw:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if char == "," and depth == 0:
            found.append(current.strip())
            current = ""
        else:
            current += char

    found.append(current.strip())

    # A generic constraint follows the base list on the same line, and its
    # first token would otherwise be read as another base type.
    cleaned = []
    for base in found:
        base = re.split(r"\bwhere\b", base)[0].strip()
        if base:
            cleaned.append(base)

    return cleaned


def _member(line: str, number: int, lines: list[str], prefix: str) -> Symbol | None:
    """One member of a type: a method, a constructor, a property or a field."""
    owner = prefix.rsplit(".", 1)[-1]

    method = _METHOD_RE.match(line)
    if method and method.group(2) not in _KEYWORDS:
        return Symbol(
            name=method.group(2),
            kind=SymbolKind.METHOD,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.{method.group(2)}",
            parameters=_parameters(method.group(3)),
        )

    constructor = _CONSTRUCTOR_RE.match(line)
    if constructor and constructor.group(1) == owner:
        return Symbol(
            name=constructor.group(1),
            kind=SymbolKind.METHOD,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.{constructor.group(1)}",
            parameters=_parameters(constructor.group(2)),
        )

    block_prop = _PROPERTY_NEXT_LINE_RE.match(line)
    if (
        block_prop
        and block_prop.group(2) not in _KEYWORDS
        and _next_code_line(lines, number).startswith("{")
    ):
        return Symbol(
            name=block_prop.group(2),
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.{block_prop.group(2)}",
        )

    operator = _OPERATOR_RE.match(line)
    if operator:
        name = f"operator {operator.group(1)}"
        return Symbol(
            name=name,
            kind=SymbolKind.METHOD,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.{name}",
            parameters=_parameters(operator.group(2)),
        )

    indexer = _INDEXER_RE.match(line)
    if indexer:
        return Symbol(
            name="this[]",
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.this[]",
            parameters=_parameters(indexer.group(1)),
        )

    prop = _PROPERTY_RE.match(line)
    if prop and prop.group(2) not in _KEYWORDS:
        return Symbol(
            name=prop.group(2),
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=_end_of_block(lines, number),
            qualified_name=f"{prefix}.{prop.group(2)}",
        )

    field = _FIELD_RE.match(line)
    if field and field.group(2) not in _KEYWORDS:
        return Symbol(
            name=field.group(2),
            kind=SymbolKind.VARIABLE,
            line=number,
            end_line=number,
            qualified_name=f"{prefix}.{field.group(2)}",
        )

    return None


def _logical_line(lines: list[str], number: int) -> str:
    """One declaration, however many lines it is written across.

    Only joins while a parameter list is still open, so an ordinary one-line
    declaration is returned untouched and nothing is joined that was not
    already unfinished.
    """
    text = lines[number - 1]
    if text.count("(") <= text.count(")"):
        return text

    joined = text
    for extra in lines[number : number + MAX_JOINED_LINES]:
        joined = f"{joined} {extra.strip()}"
        if joined.count("(") <= joined.count(")"):
            break

    return joined


def _next_code_line(lines: list[str], after: int) -> str:
    """The next line with anything on it, stripped. "" at the end of the file."""
    for line in lines[after:]:
        if line.strip():
            return line.strip()
    return ""


def _collect_dynamic_prefixes(text: str) -> set[str]:
    """String literals concatenated with something else, as in Python.

    Reflection in C# is written with strings the same way: GetMethod("On" +
    name) hides a definition from any static scan.
    """
    prefixes = set()
    for match in re.finditer(r'"([^"\n]{3,})"\s*\+', text):
        prefixes.add(match.group(1))
    for match in re.finditer(r'\$"([^"{\n]{3,})\{', text):
        prefixes.add(match.group(1))
    return prefixes
