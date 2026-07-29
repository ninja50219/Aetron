"""Tests for reducing a scan and analysis to a project summary."""

from aetron.analyzer import analyze
from aetron.context.summary import TOP_N, build_summary
from aetron.scanner import scan


def summarise(make_project, layout, name="project"):
    root = make_project(layout, name=name)
    scan_result = scan(root)
    return build_summary(scan_result, analyze(scan_result))


class TestSize:
    def test_counts_files_and_lines(self, make_project):
        summary = summarise(make_project, {"a.py": "x = 1\n", "b.py": "x = 1\ny = 2\n"})
        assert summary.file_count == 2
        assert summary.line_count == 5  # two lines plus a trailing newline each

    def test_languages_are_counted(self, make_project):
        layout = {"a.py": "x = 1\n", "b.py": "x = 1\n", "c.js": "let x = 1;\n"}
        summary = summarise(make_project, layout)
        assert summary.languages == {"python": 2, "javascript": 1}

    def test_primary_language_is_the_most_common(self, make_project):
        layout = {"a.py": "x = 1\n", "b.js": "let x = 1;\n", "c.js": "let y = 2;\n"}
        assert summarise(make_project, layout).primary_language == "javascript"

    def test_primary_language_of_an_empty_project(self, make_project):
        assert summarise(make_project, {"README.md": "# Title\n"}).primary_language is None

    def test_project_name_comes_from_the_directory(self, make_project):
        assert summarise(make_project, {"a.py": "x = 1\n"}, name="my-app").name == "my-app"


class TestStructure:
    def test_symbol_counts(self, make_project):
        layout = {"a.py": "class A:\n    def m(self):\n        pass\n\ndef f():\n    pass\n"}
        summary = summarise(make_project, layout)
        assert summary.symbol_counts == {"class": 1, "method": 1, "function": 1}

    def test_kinds_with_no_symbols_are_omitted(self, make_project):
        summary = summarise(make_project, {"a.py": "def f():\n    pass\n"})
        assert "class" not in summary.symbol_counts

    def test_import_edges_are_counted(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "import c\n", "c.py": "x = 1\n"}
        assert summarise(make_project, layout).import_edges == 2

    def test_entry_points_are_listed(self, make_project):
        layout = {"main.py": "import helper\n", "helper.py": "x = 1\n"}
        assert summarise(make_project, layout).entry_points == ["main.py"]


class TestKeyFiles:
    def test_most_depended_on_file_comes_first(self, make_project):
        layout = {"core.py": "x = 1\n", "a.py": "import core\n", "b.py": "import core\n"}
        key_files = summarise(make_project, layout).key_files
        assert key_files[0].rel_path == "core.py"
        assert key_files[0].dependents == 2

    def test_symbol_count_breaks_the_tie(self, make_project):
        # Nothing imports either file, so structure decides.
        layout = {
            "small.py": "x = 1\n",
            "big.py": "class A:\n    pass\n\nclass B:\n    pass\n\ndef f():\n    pass\n",
        }
        assert summarise(make_project, layout).key_files[0].rel_path == "big.py"

    def test_dependency_direction_is_recorded(self, make_project):
        layout = {"core.py": "x = 1\n", "app.py": "import core\n"}
        by_path = {f.rel_path: f for f in summarise(make_project, layout).key_files}
        assert by_path["app.py"].dependencies == 1
        assert by_path["app.py"].dependents == 0
        assert by_path["core.py"].dependents == 1

    def test_list_is_capped(self, make_project):
        layout = {f"file{i}.py": "x = 1\n" for i in range(TOP_N + 5)}
        assert len(summarise(make_project, layout).key_files) == TOP_N


class TestContext:
    def test_dependencies_are_grouped_by_ecosystem(self, make_project):
        layout = {
            "package.json": '{"dependencies": {"react": "^18.0.0"}}',
            "requirements.txt": "flask==3.0.0\nrequests>=2.31\n",
        }
        summary = summarise(make_project, layout)
        assert summary.ecosystems == {"node": 1, "python": 2}

    def test_notable_dependencies_carry_versions(self, make_project):
        layout = {"requirements.txt": "flask==3.0.0\n"}
        assert summarise(make_project, layout).notable_dependencies == ["flask ==3.0.0"]

    def test_duplicate_dependency_named_once(self, make_project):
        # A monorepo declares the same package in several manifests.
        layout = {
            "web/package.json": '{"dependencies": {"react": "^18.0.0"}}',
            "admin/package.json": '{"dependencies": {"react": "^18.0.0"}}',
        }
        assert summarise(make_project, layout).notable_dependencies == ["react ^18.0.0"]

    def test_documentation_is_listed(self, make_project):
        layout = {"README.md": "# Title\n", "app.py": "x = 1\n"}
        assert summarise(make_project, layout).documentation == ["README.md"]


class TestFindings:
    def test_insights_are_included(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "import a\n"}
        assert [i.kind for i in summarise(make_project, layout).insights] == ["circular-import"]

    def test_unparsed_languages_are_reported(self, make_project):
        # Being explicit about coverage beats letting a reader assume it.
        layout = {"a.py": "x = 1\n", "Main.cs": "class M {}\n", "Other.cs": "class O {}\n"}
        assert summarise(make_project, layout).unparsed_languages == {".cs": 2}

    def test_fully_parsed_project_reports_none(self, make_project):
        assert summarise(make_project, {"a.py": "x = 1\n"}).unparsed_languages == {}
