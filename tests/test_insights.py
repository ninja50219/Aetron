"""Tests for structural findings derived from the import graph."""

from aetron.analyzer import analyze
from aetron.context.insights import Severity, find_cycles, find_insights
from aetron.scanner import scan


def insights_for(make_project, layout):
    return find_insights(analyze(scan(make_project(layout))))


def kinds(found):
    return {i.kind for i in found}


def of_kind(found, kind):
    return [i for i in found if i.kind == kind]


class TestCycleDetection:
    def test_no_cycle(self):
        assert find_cycles({"a": {"b"}, "b": {"c"}, "c": set()}) == []

    def test_two_file_cycle(self):
        assert find_cycles({"a": {"b"}, "b": {"a"}}) == [["a", "b"]]

    def test_longer_cycle(self):
        assert find_cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}}) == [["a", "b", "c"]]

    def test_two_independent_cycles(self):
        graph = {"a": {"b"}, "b": {"a"}, "c": {"d"}, "d": {"c"}}
        assert find_cycles(graph) == [["a", "b"], ["c", "d"]]

    def test_self_loop_is_not_a_cycle(self):
        # The analyzer already drops self-imports; one file is not a cycle.
        assert find_cycles({"a": {"a"}}) == []

    def test_node_outside_any_cycle_is_untouched(self):
        graph = {"a": {"b"}, "b": {"a"}, "c": {"a"}}
        assert find_cycles(graph) == [["a", "b"]]

    def test_deep_chain_does_not_hit_the_recursion_limit(self):
        # Written iteratively for exactly this case: a long import chain in a
        # large project would overflow a recursive implementation.
        depth = 5000
        graph = {str(i): {str(i + 1)} for i in range(depth)}
        graph[str(depth)] = {"0"}
        cycles = find_cycles(graph)
        assert len(cycles) == 1
        assert len(cycles[0]) == depth + 1


class TestCircularImports:
    def test_reported_from_real_files(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "import a\n"}
        found = of_kind(insights_for(make_project, layout), "circular-import")
        assert len(found) == 1
        assert found[0].files == ["a.py", "b.py"]

    def test_two_file_cycle_is_medium_severity(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "import a\n"}
        found = of_kind(insights_for(make_project, layout), "circular-import")
        assert found[0].severity == Severity.MEDIUM

    def test_longer_cycle_is_high_severity(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "import c\n", "c.py": "import a\n"}
        found = of_kind(insights_for(make_project, layout), "circular-import")
        assert found[0].severity == Severity.HIGH

    def test_clean_project_reports_nothing(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "x = 1\n"}
        assert "circular-import" not in kinds(insights_for(make_project, layout))


class TestOrphanModules:
    def test_disconnected_file_is_reported(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "x = 1\n", "lonely.py": "def f():\n    pass\n"}
        found = of_kind(insights_for(make_project, layout), "orphan-module")
        assert found[0].files == ["lonely.py"]

    def test_empty_init_is_not_an_orphan(self, make_project):
        # A package marker has no symbols and is not a leftover.
        layout = {"pkg/__init__.py": "", "pkg/a.py": "import b\n", "b.py": "x = 1\n"}
        found = of_kind(insights_for(make_project, layout), "orphan-module")
        assert found == [] or "pkg/__init__.py" not in found[0].files


class TestHubFiles:
    def test_widely_imported_file_is_reported(self, make_project):
        layout = {"core.py": "x = 1\n"}
        for i in range(9):
            layout[f"user{i}.py"] = "import core\n"
        found = of_kind(insights_for(make_project, layout), "hub-file")
        assert found[0].files[0] == "core.py"

    def test_small_project_reports_no_hubs(self, make_project):
        layout = {"a.py": "import b\n", "b.py": "x = 1\n"}
        assert "hub-file" not in kinds(insights_for(make_project, layout))


class TestOrdering:
    def test_most_severe_first(self, make_project):
        layout = {
            "a.py": "import b\n",
            "b.py": "import c\n",
            "c.py": "import a\n",
            "lonely.py": "def f():\n    pass\n",
        }
        found = insights_for(make_project, layout)
        assert found[0].severity == Severity.HIGH
