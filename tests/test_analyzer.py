"""End-to-end tests for scan() feeding analyze()."""

from aetron.analyzer import analyze
from aetron.analyzer.symbols import SymbolKind
from aetron.scanner import scan


def analyze_project(root):
    return analyze(scan(root))


class TestSymbolCollection:
    def test_symbols_from_every_file(self, make_project):
        root = make_project(
            {"a.py": "class A:\n    def m(self):\n        pass\n", "b.py": "def f():\n    pass\n"}
        )
        result = analyze_project(root)
        assert result.count_of(SymbolKind.CLASS) == 1
        assert result.count_of(SymbolKind.METHOD) == 1
        assert result.count_of(SymbolKind.FUNCTION) == 1

    def test_find_by_plain_name(self, make_project):
        root = make_project({"a.py": "def target():\n    pass\n"})
        found = analyze_project(root).find("target")
        assert len(found) == 1
        assert found[0][1].line == 1

    def test_find_by_qualified_name(self, make_project):
        root = make_project({"a.py": "class A:\n    def m(self):\n        pass\n"})
        assert len(analyze_project(root).find("A.m")) == 1

    def test_same_name_in_two_files(self, make_project):
        root = make_project({"a.py": "def run():\n    pass\n", "b.py": "def run():\n    pass\n"})
        assert len(analyze_project(root).find("run")) == 2

    def test_unsupported_language_is_listed_not_parsed(self, make_project):
        root = make_project({"a.py": "x = 1\n", "Main.cs": "class Main {}\n"})
        result = analyze_project(root)
        assert [f.rel_path for f in result.files] == ["a.py"]
        assert result.unparsed == ["Main.cs"]


class TestImportGraph:
    def test_edge_between_project_files(self, make_project):
        root = make_project({"app.py": "import helper\n", "helper.py": "x = 1\n"})
        result = analyze_project(root)
        assert result.imports["app.py"] == {"helper.py"}
        assert result.imported_by["helper.py"] == {"app.py"}

    def test_external_import_is_not_an_edge(self, make_project):
        root = make_project({"app.py": "import os\nimport json\n"})
        assert analyze_project(root).imports["app.py"] == set()

    def test_package_relative_import(self, make_project):
        root = make_project(
            {
                "pkg/__init__.py": "",
                "pkg/core.py": "from .util import helper\n",
                "pkg/util.py": "def helper():\n    pass\n",
            }
        )
        assert analyze_project(root).imports["pkg/core.py"] == {"pkg/util.py"}

    def test_from_package_import_module(self, make_project):
        root = make_project(
            {"pkg/__init__.py": "", "pkg/core.py": "x = 1\n", "app.py": "from pkg import core\n"}
        )
        # The submodule, not the package __init__.
        assert analyze_project(root).imports["app.py"] == {"pkg/core.py"}

    def test_self_import_is_not_an_edge(self, make_project):
        root = make_project({"app.py": "import app\n"})
        assert analyze_project(root).imports["app.py"] == set()

    def test_entry_points_are_files_nobody_imports(self, make_project):
        root = make_project({"main.py": "import helper\n", "helper.py": "x = 1\n"})
        assert analyze_project(root).entry_points() == ["main.py"]

    def test_every_file_has_a_graph_entry(self, make_project):
        root = make_project({"lonely.py": "x = 1\n"})
        result = analyze_project(root)
        assert result.imports["lonely.py"] == set()
        assert result.imported_by["lonely.py"] == set()


class TestRobustness:
    def test_broken_file_does_not_stop_the_others(self, make_project):
        root = make_project({"broken.py": "def f(\n", "fine.py": "def g():\n    pass\n"})
        result = analyze_project(root)
        assert [path for path, _ in result.parse_errors] == ["broken.py"]
        assert len(result.find("g")) == 1

    def test_progress_callback(self, make_project):
        root = make_project({"a.py": "x = 1\n", "b.py": "x = 1\n"})
        seen = []
        analyze(scan(root), on_progress=lambda n, path: seen.append(n))
        assert seen == [1, 2]
