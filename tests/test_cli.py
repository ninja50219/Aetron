"""Tests for the command line, including the three retrieval levels.

The CLI is the only consumer of the pipeline that exists so far, so these are
also the tests that the levels fit together: what search prints as a path is
what structure accepts, and what structure prints as a name is what source
accepts. A protocol whose levels do not compose is not a protocol.
"""

import json

import pytest

from aetron.cli.main import build_parser, main


@pytest.fixture
def run(make_project, monkeypatch, capsys):
    """Run the CLI against a throwaway project and return what it printed."""

    def _run(layout, *argv, name="project"):
        root = make_project(layout, name=name)
        monkeypatch.setattr("sys.argv", ["aetron", *argv[:1], str(root), *argv[1:]])
        main()
        return capsys.readouterr().out

    return _run


PROJECT = {
    "auth/login.py": (
        "import hashlib\n"
        "\n"
        "\n"
        "class LoginController:\n"
        '    """Handles signing in."""\n'
        "\n"
        "    def login_handler(self, user, password):\n"
        '        """Check a password and start a session."""\n'
        "        return hashlib.sha256(password.encode()).hexdigest()\n"
    ),
    "ui/view.py": "def render():\n    return 'page'\n",
    "README.md": "# Demo\n",
}


class TestParser:
    def test_every_command_is_registered(self):
        parser = build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert set(actions[0].choices) == {
            "scan", "analyze", "summary", "search", "structure", "source", "ask"
        }


class TestExistingCommands:
    def test_scan_reports_files(self, run):
        assert "2 files" in run(PROJECT, "scan") or "Scanned" in run(PROJECT, "scan")

    def test_analyze_reports_symbols(self, run):
        output = run(PROJECT, "analyze")
        assert "class" in output

    def test_summary_reaches_the_context_stage(self, run):
        """The context stage had no consumer at all before this command."""
        output = run(PROJECT, "summary")
        assert "project" in output
        assert "Start reading here" in output


class TestLevelOne:
    def test_search_ranks_candidates(self, run):
        output = run(PROJECT, "search", "login")
        assert "auth/login.py" in output
        assert "%" in output

    def test_search_says_when_nothing_matches(self, run):
        assert "Nothing in this project" in run(PROJECT, "search", "kubernetes")

    def test_search_returns_no_source_code(self, run):
        assert "sha256" not in run(PROJECT, "search", "login")

    def test_the_limit_is_honoured(self, run):
        output = run(PROJECT, "search", "login", "--limit", "1")
        assert output.count("%") == 1

    def test_json_output_is_parsable(self, run):
        found = json.loads(run(PROJECT, "search", "login", "--json"))
        assert found[0]["rel_path"] == "auth/login.py"
        assert 0 < found[0]["percent"] <= 99


class TestLevelTwo:
    def test_structure_lists_definitions_without_bodies(self, run):
        output = run(PROJECT, "structure", "auth/login.py")
        assert "class LoginController" in output
        assert "login_handler" in output
        assert "sha256" not in output

    def test_structure_reports_a_file_it_cannot_read(self, run):
        layout = {"login.go": "package main\n\nfunc Login() {}\n"}
        assert "no parser" in run(layout, "structure", "login.go")

    def test_json_output_is_parsable(self, run):
        data = json.loads(run(PROJECT, "structure", "auth/login.py", "--json"))
        assert data["rel_path"] == "auth/login.py"
        assert any(s["name"] == "login_handler" for s in data["symbols"])


class TestLevelThree:
    def test_source_returns_the_definition(self, run):
        output = run(PROJECT, "source", "auth/login.py", "LoginController.login_handler")
        assert "sha256" in output
        assert "auth/login.py:7" in output

    def test_source_numbers_its_lines(self, run):
        output = run(PROJECT, "source", "auth/login.py", "login_handler")
        assert "7|" in output

    def test_source_reports_an_unknown_name(self, run):
        assert "no definition called" in run(PROJECT, "source", "auth/login.py", "nope")

    def test_json_output_is_parsable(self, run):
        data = json.loads(run(PROJECT, "source", "auth/login.py", "login_handler", "--json"))
        assert data["location"] == "auth/login.py:7"
        assert "sha256" in data["text"]


class TestTheLevelsCompose:
    """What one level prints, the next level accepts."""

    def test_a_search_result_is_a_valid_structure_argument(self, run):
        found = json.loads(run(PROJECT, "search", "login", "--json"))
        output = run(PROJECT, "structure", found[0]["rel_path"])
        assert "unavailable" not in output

    def test_a_structure_name_is_a_valid_source_argument(self, run):
        data = json.loads(run(PROJECT, "structure", "auth/login.py", "--json"))
        name = next(s for s in data["symbols"] if s["name"] == "login_handler")
        result = json.loads(
            run(PROJECT, "source", "auth/login.py", name["qualified_name"], "--json")
        )
        assert result["problem"] == ""
        assert result["text"]

    def test_each_level_costs_more_than_the_last(self, run):
        """The protocol only pays for itself if this ordering holds."""
        one = run(PROJECT, "search", "login")
        two = run(PROJECT, "structure", "auth/login.py")
        three = len(PROJECT["auth/login.py"])
        assert len(one) < three
        assert len(two) < three
