"""Exercise conversations because navigation errors cross prompt boundaries.

Real retrieval fixtures verify that numbers preserve identifiers all the way
to source. Temporary history keeps tests out of the reader's own projects.
"""

import json
import sys
from unittest.mock import Mock

import pytest

from aetron.cli import interactive as ui
from aetron.cli.main import main


@pytest.fixture
def conversation(monkeypatch, tmp_path):
    history = tmp_path / "config" / "recent-projects.json"

    def run(*answers):
        replies = iter(answers)
        monkeypatch.setattr("builtins.input", lambda _: next(replies))
        ui.run(history)

    return run, history


def test_numbers_reach_source_and_scan_only_once(conversation, make_project, monkeypatch, capsys):
    root = make_project({"server/Services/combat.py": "def combat():\n    return 12345\n"})
    run, history = conversation
    scan = Mock(wraps=ui.scan)
    analyze = Mock(wraps=ui.analyze)
    monkeypatch.setattr(ui, "scan", scan)
    monkeypatch.setattr(ui, "analyze", analyze)
    run("1", str(root), "1", "2", "combat", "1", "1", "0", "0", "0", "0")
    output = capsys.readouterr().out
    assert "return 12345" in output
    assert "server/Services/combat.py:1" in output
    assert "Limitations" in output
    assert scan.call_count == analyze.call_count == 1
    assert json.loads(history.read_text()) == [str(root)]
    run("1", "0", "0")
    assert scan.call_count == 2


def test_refresh_is_explicit_and_warning_is_once(conversation, make_project, monkeypatch, capsys):
    root = make_project({"app.py": "def hello(): pass\n"})
    run, _ = conversation
    scan = Mock(wraps=ui.scan)
    monkeypatch.setattr(ui, "scan", scan)
    monkeypatch.setattr(ui, "gitignore_available", False)
    run("1", str(root), "4", "1", "0", "1", "0", "0")
    captured = capsys.readouterr()
    assert scan.call_count == 3
    assert captured.err.count("pathspec is not installed") == 1
    assert "pathspec" not in captured.out


def test_invalid_choices_and_path_recover(conversation, make_project, capsys):
    root = make_project({"app.py": "x = 1\n"})
    run, _ = conversation
    run("", "abc", "999", "1", "C:/.../missing-project", "1", str(root),
        "2", "", "2", "nothing_matches", "0", "0")
    output = capsys.readouterr().out
    assert output.count("Enter a number") == 3
    assert "Cannot open project" in output
    assert "Enter a word" in output
    assert "Nothing in this project" in output


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_cancellation_at_nested_prompt(conversation, make_project, monkeypatch, error, capsys):
    root = make_project({"app.py": "x = 1\n"})
    _, history = conversation
    replies = iter(["1", str(root), "2"])

    def reply(_):
        try:
            return next(replies)
        except StopIteration:
            raise error

    monkeypatch.setattr("builtins.input", reply)
    ui.run(history)
    assert "Goodbye" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{", "{}", '[3]', '["project", null]'])
def test_corrupt_history_is_reported(tmp_path, content, capsys):
    path = tmp_path / "history.json"
    path.write_text(content)
    assert ui.load_history(path) == []
    assert "Cannot read recent projects" in capsys.readouterr().err


def test_failed_save_preserves_previous_history(tmp_path, monkeypatch, capsys):
    path = tmp_path / "history.json"
    ui.save_history(path, ["old"])
    monkeypatch.setattr(ui.os, "replace", Mock(side_effect=PermissionError("denied")))
    ui.save_history(path, ["new"])
    assert ui.load_history(path) == ["old"]
    assert list(tmp_path.iterdir()) == [path]
    assert "Cannot save recent projects" in capsys.readouterr().err


def test_missing_recent_project_stays_selectable(conversation, tmp_path, capsys):
    run, history = conversation
    missing = str(tmp_path / "gone")
    ui.save_history(history, [missing])
    run("1", "0")
    assert "Cannot open project" in capsys.readouterr().out
    assert ui.load_history(history) == [missing]


def test_no_arguments_dispatch_to_menu(monkeypatch):
    run = Mock()
    monkeypatch.setattr(ui, "run", run)
    monkeypatch.setattr(sys, "argv", ["aetron"])
    main()
    run.assert_called_once_with()


@pytest.mark.parametrize("source", ["package combat\n", "# combat\n"])
def test_unavailable_or_empty_file_returns_to_candidates(conversation, make_project, source, capsys):
    extension = "go" if source.startswith("package") else "py"
    root = make_project({f"combat.{extension}": source})
    run, _ = conversation
    run("1", str(root), "2", "combat", "1", "0", "0", "0")
    output = capsys.readouterr().out
    assert "no parser" in output or "no definitions" in output


def test_duplicate_definitions_never_return_wrong_source(conversation, make_project, capsys):
    root = make_project({"combat.py": "def combat(): return 111\ndef combat(): return 222\n"})
    run, _ = conversation
    run("1", str(root), "2", "combat", "1", "2", "0", "0", "0", "0")
    output = capsys.readouterr().out
    assert "cannot select one by line number" in output
    assert "return 111" not in output
    assert "return 222" not in output


def test_refresh_failure_keeps_previous_index(conversation, make_project, monkeypatch, capsys):
    root = make_project({"combat.py": "def combat(): return 123\n"})
    run, _ = conversation
    scanned = ui.scan(root)
    monkeypatch.setattr(ui, "scan", Mock(side_effect=[scanned, OSError("unavailable")]))
    run("1", str(root), "4", "2", "combat", "1", "1", "0", "0", "0", "0")
    output = capsys.readouterr().out
    assert "Could not complete that action" in output
    assert "return 123" in output
