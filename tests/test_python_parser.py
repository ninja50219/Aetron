"""Tests for extracting symbols from Python source."""

from aetron.analyzer.python_parser import parse
from aetron.analyzer.symbols import SymbolKind


def named(result, name):
    """The one symbol with this name. Every file also carries a symbol for the
    module itself, so indexing by position no longer identifies a definition."""
    return next(s for s in result.symbols if s.name == name)


def definitions(result):
    """Every symbol except the one standing for the module."""
    return [s for s in result.symbols if s.kind != SymbolKind.MODULE]

def kinds(result, kind):
    return [s.qualified_name for s in result.of_kind(kind)]


class TestDefinitions:
    def test_module_level_function(self):
        result = parse("def run():\n    pass\n", "a.py")
        assert kinds(result, SymbolKind.FUNCTION) == ["run"]

    def test_class_and_its_methods(self):
        source = "class Scanner:\n    def scan(self):\n        pass\n"
        result = parse(source, "a.py")
        assert kinds(result, SymbolKind.CLASS) == ["Scanner"]
        # A function inside a class is a method, and carries the class name.
        assert kinds(result, SymbolKind.METHOD) == ["Scanner.scan"]

    def test_async_function(self):
        assert kinds(parse("async def fetch():\n    pass\n", "a.py"), SymbolKind.FUNCTION) == [
            "fetch"
        ]

    def test_nested_class(self):
        source = "class Outer:\n    class Inner:\n        pass\n"
        assert kinds(parse(source, "a.py"), SymbolKind.CLASS) == ["Outer", "Outer.Inner"]

    def test_base_classes_are_recorded(self):
        source = "class Child(Base, module.Other):\n    pass\n"
        symbol = parse(source, "a.py").of_kind(SymbolKind.CLASS)[0]
        assert symbol.bases == ["Base", "module.Other"]

    def test_parameters_including_varargs(self):
        source = "def f(a, b=1, *args, c, **kwargs):\n    pass\n"
        symbol = parse(source, "a.py").of_kind(SymbolKind.FUNCTION)[0]
        assert symbol.parameters == ["a", "b", "c", "*args", "**kwargs"]

    def test_docstring_is_captured(self):
        result = parse('def f():\n    """Does a thing."""\n', "a.py")
        assert named(result, "f").docstring == "Does a thing."

    def test_line_range(self):
        result = parse("\n\ndef f():\n    x = 1\n    return x\n", "a.py")
        symbol = named(result, "f")
        assert (symbol.line, symbol.end_line) == (3, 5)


class TestVariables:
    def test_module_level_assignment(self):
        assert kinds(parse("LIMIT = 10\n", "a.py"), SymbolKind.VARIABLE) == ["LIMIT"]

    def test_annotated_assignment(self):
        assert kinds(parse("LIMIT: int = 10\n", "a.py"), SymbolKind.VARIABLE) == ["LIMIT"]

    def test_tuple_unpacking(self):
        assert kinds(parse("A, B = 1, 2\n", "a.py"), SymbolKind.VARIABLE) == ["A", "B"]

    def test_locals_are_not_indexed(self):
        # Locals would bury the index in noise without adding structure.
        source = "def f():\n    temporary = 1\n    return temporary\n"
        assert kinds(parse(source, "a.py"), SymbolKind.VARIABLE) == []

    def test_class_attributes_are_not_module_variables(self):
        source = "class A:\n    attribute = 1\n"
        assert kinds(parse(source, "a.py"), SymbolKind.VARIABLE) == []


class TestImports:
    def test_plain_import(self):
        reference = parse("import os\n", "a.py").imports[0]
        assert (reference.module, reference.level, reference.from_import) == ("os", 0, False)

    def test_several_modules_in_one_statement(self):
        refs = parse("import os, sys\n", "a.py").imports
        assert [r.module for r in refs] == ["os", "sys"]

    def test_from_import_records_names(self):
        reference = parse("from pathlib import Path, PurePath\n", "a.py").imports[0]
        assert reference.module == "pathlib"
        assert reference.names == ["Path", "PurePath"]
        assert reference.from_import

    def test_relative_import_level(self):
        reference = parse("from ..pkg import thing\n", "a.py").imports[0]
        assert (reference.module, reference.level) == ("pkg", 2)
        assert reference.is_relative

    def test_bare_relative_import(self):
        reference = parse("from . import sibling\n", "a.py").imports[0]
        assert (reference.module, reference.level, reference.names) == ("", 1, ["sibling"])


class TestRobustness:
    def test_syntax_error_is_recorded_not_raised(self):
        result = parse("def broken(\n", "a.py")
        assert result.parse_error is not None
        assert result.symbols == []

    def test_empty_file(self):
        result = parse("", "a.py")
        assert result.parse_error is None
        assert definitions(result) == []


class TestModuleSymbol:
    """Every parsed file carries a symbol for itself, holding the one sentence
    that says what the whole file is for."""

    def test_the_module_is_a_symbol(self):
        result = parse('"""What this file is for."""\n', "pkg/thing.py")
        module = next(s for s in result.symbols if s.kind == SymbolKind.MODULE)
        assert module.name == "thing"
        assert module.docstring == "What this file is for."

    def test_a_file_without_a_docstring_still_has_one(self):
        result = parse("x = 1\n", "a.py")
        module = next(s for s in result.symbols if s.kind == SymbolKind.MODULE)
        assert module.docstring is None

    def test_the_module_spans_the_whole_file(self):
        result = parse("x = 1\ny = 2\nz = 3\n", "a.py")
        module = next(s for s in result.symbols if s.kind == SymbolKind.MODULE)
        assert (module.line, module.end_line) == (1, 3)

    def test_a_file_that_will_not_parse_has_no_module_symbol(self):
        assert parse("def broken(:\n", "a.py").symbols == []
