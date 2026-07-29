"""Extracting symbols from Python source with the standard ast module.

Python is the one language available without a third-party parser, so it sets
the shape every other language parser will follow: take text, return
FileSymbols, never raise.
"""

import ast

from .symbols import FileSymbols, ImportRef, Symbol, SymbolKind

LANGUAGE = "python"


def parse(text: str, rel_path: str) -> FileSymbols:
    """Parse Python source into symbols and imports.

    Syntax errors are recorded rather than raised: one unparseable file in a
    large project must not stop the analysis of the rest.
    """
    result = FileSymbols(rel_path=rel_path, language=LANGUAGE)

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        result.parse_error = str(exc)
        return result

    _visit_body(tree.body, result, prefix="")
    result.references = _collect_references(tree)
    return result


def _collect_references(tree: ast.AST) -> set[str]:
    """Every name the file reads, for answering "is this used anywhere?".

    Attribute access contributes both the attribute and the dotted form, so
    "self.scan()" marks "scan" used and "config.LIMIT" marks "LIMIT" used.
    Only Load context counts: a name being assigned is a definition, not a use.
    """
    references: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            references.add(node.id)
        elif isinstance(node, ast.Attribute):
            references.add(node.attr)
            references.add(_expression_name(node))
        elif isinstance(node, ast.ImportFrom):
            # "from x import Thing" is a use of Thing, not a definition.
            references.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # __all__ entries, getattr() targets and similar string references
            # are the main source of false "unused" reports.
            references.add(node.value)

    return references


def _visit_body(body: list[ast.stmt], result: FileSymbols, prefix: str) -> None:
    """Walk one block, recursing into classes to reach methods."""
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            result.imports.extend(_build_imports(node))

        elif isinstance(node, ast.ClassDef):
            symbol = _build_class(node, prefix)
            result.symbols.append(symbol)
            # Nested definitions get the class name as their prefix, which is
            # what turns a function into a method.
            _visit_body(node.body, result, prefix=symbol.qualified_name)

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            result.symbols.append(_build_function(node, prefix))

        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not prefix:
            # Only module-level assignments; locals are noise in an index.
            result.symbols.extend(_build_variables(node))


def _qualify(name: str, prefix: str) -> str:
    return f"{prefix}.{name}" if prefix else name


def _build_imports(node: ast.Import | ast.ImportFrom) -> list[ImportRef]:
    if isinstance(node, ast.Import):
        # "import os, sys" is two independent modules, so it becomes two refs
        # rather than one ref with a list meaning something different.
        return [
            ImportRef(module=alias.name, line=node.lineno) for alias in node.names
        ]

    return [
        ImportRef(
            module=node.module or "",
            names=[alias.name for alias in node.names],
            level=node.level,
            line=node.lineno,
            from_import=True,
        )
    ]


def _build_class(node: ast.ClassDef, prefix: str) -> Symbol:
    return Symbol(
        name=node.name,
        kind=SymbolKind.CLASS,
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        qualified_name=_qualify(node.name, prefix),
        bases=[_expression_name(base) for base in node.bases],
        docstring=ast.get_docstring(node),
    )


def _build_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str
) -> Symbol:
    args = node.args
    parameters = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if args.vararg:
        parameters.append(f"*{args.vararg.arg}")
    if args.kwarg:
        parameters.append(f"**{args.kwarg.arg}")

    return Symbol(
        name=node.name,
        kind=SymbolKind.METHOD if prefix else SymbolKind.FUNCTION,
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        qualified_name=_qualify(node.name, prefix),
        parameters=parameters,
        docstring=ast.get_docstring(node),
    )


def _build_variables(node: ast.Assign | ast.AnnAssign) -> list[Symbol]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    found = []

    for target in targets:
        for name in _target_names(target):
            found.append(
                Symbol(
                    name=name,
                    kind=SymbolKind.VARIABLE,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    qualified_name=name,
                )
            )

    return found


def _target_names(target: ast.expr) -> list[str]:
    """Names bound by an assignment target, unpacking tuples."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for element in target.elts:
            names.extend(_target_names(element))
        return names
    return []


def _expression_name(node: ast.expr) -> str:
    """Render a base class expression back to something readable."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_expression_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return _expression_name(node.value)
    return ast.unparse(node)
