"""Tests for level 2 of the retrieval protocol: the shape of one file."""

import json

from aetron.analyzer import analyze
from aetron.context.structure import SUMMARY_LIMIT, build_structure, render, summarise
from aetron.scanner import scan


def structure_of(make_project, layout, rel_path):
    root = make_project(layout)
    scan_result = scan(root)
    return build_structure(scan_result, analyze(scan_result), rel_path)


def named(structure, name):
    return next(s for s in structure.symbols if s.name == name)


class TestSummarising:
    def test_the_first_line_is_the_summary(self):
        assert summarise("Do the thing.\n\nAt length, over pages.") == "Do the thing."

    def test_no_docstring_is_an_empty_summary(self):
        assert summarise(None) == ""

    def test_a_long_first_line_is_truncated(self):
        assert len(summarise("word " * 200)) <= SUMMARY_LIMIT


class TestSkeleton:
    def test_definitions_are_listed_with_their_lines(self, make_project):
        layout = {"a.py": "def login():\n    return 1\n"}
        symbol = named(structure_of(make_project, layout, "a.py"), "login")
        assert (symbol.line, symbol.end_line) == (1, 2)

    def test_parameters_are_kept(self, make_project):
        layout = {"a.py": "def login(user, password):\n    pass\n"}
        symbol = named(structure_of(make_project, layout, "a.py"), "login")
        assert symbol.parameters == ["user", "password"]

    def test_methods_carry_their_class(self, make_project):
        layout = {"a.py": "class Login:\n    def handle(self):\n        pass\n"}
        symbol = named(structure_of(make_project, layout, "a.py"), "handle")
        assert symbol.qualified_name == "Login.handle"

    def test_base_classes_are_kept(self, make_project):
        layout = {"a.py": "class Login(Controller):\n    pass\n"}
        assert named(structure_of(make_project, layout, "a.py"), "Login").bases == ["Controller"]

    def test_imports_are_listed(self, make_project):
        layout = {"a.py": "import os\nfrom pathlib import Path\n"}
        imports = structure_of(make_project, layout, "a.py").imports
        assert "os" in imports
        assert "pathlib (Path)" in imports

    def test_project_neighbours_are_listed_both_ways(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "x = 1\n"}
        assert structure_of(make_project, layout, "a.py").imports_files == ["b.py"]
        assert structure_of(make_project, layout, "b.py").imported_by == ["a.py"]

    def test_the_line_count_comes_from_the_scan(self, make_project):
        layout = {"a.py": "x = 1\ny = 2\nz = 3\n"}
        assert structure_of(make_project, layout, "a.py").lines == 3


class TestNoCodeLeaks:
    """A skeleton that contained code would make level 3 pointless."""

    def test_bodies_are_not_included(self, make_project):
        layout = {"a.py": "def login():\n    secret = 'do not leak this'\n"}
        rendered = json.dumps(structure_of(make_project, layout, "a.py").to_dict())
        assert "do not leak this" not in rendered

    def test_a_skeleton_is_smaller_than_its_file(self, make_project):
        body = "\n".join(
            f"def step_{n}(value):\n"
            f'    """Step {n} of the process."""\n'
            f"    total = value * {n}\n"
            f"    if total > 10:\n"
            f"        total = 10\n"
            f"    return total\n"
            for n in range(60)
        )
        root = make_project({"a.py": body})
        scan_result = scan(root)
        skeleton = build_structure(scan_result, analyze(scan_result), "a.py")
        assert len(render(skeleton)) < len(body)

    def test_text_is_cheaper_than_json(self, make_project):
        """JSON repeats its keys per symbol; a file of many small definitions
        has more keys than code, and its JSON skeleton can exceed the file."""
        body = "\n".join(f"def step_{n}():\n    return {n}\n" for n in range(40))
        root = make_project({"a.py": body})
        scan_result = scan(root)
        skeleton = build_structure(scan_result, analyze(scan_result), "a.py")
        assert len(render(skeleton)) < len(json.dumps(skeleton.to_dict()))


class TestSayingWhenThereIsNothingToSay:
    """An empty skeleton and an unreadable file are opposite findings."""

    def test_a_file_with_no_parser_explains_itself(self, make_project):
        layout = {"login.go": "package main\n\nfunc Login() {}\n"}
        structure = structure_of(make_project, layout, "login.go")
        assert structure.available is False
        assert "no parser" in structure.unavailable

    def test_a_file_outside_the_project_is_a_different_answer(self, make_project):
        structure = structure_of(make_project, {"a.py": "x = 1\n"}, "elsewhere.py")
        assert "not a file in this project" in structure.unavailable

    def test_a_syntax_error_is_reported_as_one(self, make_project):
        layout = {"a.py": "def broken(:\n"}
        structure = structure_of(make_project, layout, "a.py")
        assert structure.available is False
        assert "could not be parsed" in structure.unavailable

    def test_a_genuinely_empty_file_is_available(self, make_project):
        structure = structure_of(make_project, {"a.py": "# nothing here\n"}, "a.py")
        assert structure.available is True
        assert structure.symbols == []


class TestJsonForm:
    def test_empty_fields_are_dropped(self, make_project):
        layout = {"a.py": "def login():\n    pass\n"}
        data = structure_of(make_project, layout, "a.py").to_dict()
        assert "imports" not in data
        assert data["symbols"][0].keys() >= {"name", "kind", "line", "end_line"}

    def test_a_qualified_name_repeating_the_name_is_dropped(self, make_project):
        layout = {"a.py": "def login():\n    pass\n"}
        data = structure_of(make_project, layout, "a.py").to_dict()
        assert "qualified_name" not in data["symbols"][0]

    def test_a_qualified_name_that_adds_something_is_kept(self, make_project):
        layout = {"a.py": "class Login:\n    def handle(self):\n        pass\n"}
        data = structure_of(make_project, layout, "a.py").to_dict()
        handle = next(s for s in data["symbols"] if s["name"] == "handle")
        assert handle["qualified_name"] == "Login.handle"

    def test_the_result_is_json_serialisable(self, make_project):
        layout = {"a.py": "class Login(Base):\n    def handle(self, user):\n        pass\n"}
        json.dumps(structure_of(make_project, layout, "a.py").to_dict())


class TestRendering:
    def test_definitions_appear_with_their_span(self, make_project):
        layout = {"a.py": "def login(user):\n    pass\n"}
        assert "function login(user) @1-2" in render(structure_of(make_project, layout, "a.py"))

    def test_a_class_shows_its_base(self, make_project):
        layout = {"a.py": "class Login(Controller):\n    pass\n"}
        assert "class Login(Controller)" in render(structure_of(make_project, layout, "a.py"))

    def test_an_unavailable_file_says_why(self, make_project):
        layout = {"login.go": "package main\n\nfunc Login() {}\n"}
        rendered = render(structure_of(make_project, layout, "login.go"))
        assert "unavailable" in rendered and "no parser" in rendered

    def test_an_empty_file_says_it_is_empty(self, make_project):
        rendered = render(structure_of(make_project, {"a.py": "# nothing\n"}, "a.py"))
        assert "no definitions" in rendered

    def test_no_bodies_are_rendered(self, make_project):
        layout = {"a.py": "def login():\n    secret = 'do not leak this'\n"}
        assert "do not leak this" not in render(structure_of(make_project, layout, "a.py"))
