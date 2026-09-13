"""Exercise explain through the real command-line entry point."""

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args, input=None):
    return subprocess.run(
        [sys.executable, "-m", "aetron", *map(str, args)],
        input=input, capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
    )


def test_explain_reports_structure_and_coverage(make_project):
    root = make_project({
        "app.py": "import helper\n",
        "helper.py": "def run():\n    return 1\n",
        "broken.py": "def broken(\n",
        "Player.cs": "class Player {}\n",
        "README.md": "# Demo\n",
        "requirements.txt": "requests>=2\n",
    }, name="project with spaces")
    result = run_cli("explain", root)
    assert result.returncode == 0, result.stderr
    for expected in ("project with spaces", "helper.py", "broken.py", ".cs", "README.md", "requests"):
        assert expected in result.stdout
    assert "Traceback" not in result.stderr


def test_explain_empty_project(make_project):
    result = run_cli("explain", make_project({}))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "  Files: 0" in lines
    assert "  Lines: 0" in lines
    assert "  Parse errors: 0" in lines


def test_broken_importer_reports_incomplete_graph(make_project):
    root = make_project({
        "pkg/app.py": "import helper\ndef broken(\n",
        "helper.py": "def run():\n    return 1\n",
    })
    result = run_cli("explain", root)
    assert result.returncode == 0, result.stderr
    assert "pkg/app.py" in result.stdout
    assert "no-incoming-imports list may be incomplete" in result.stdout
    assert "  Parse errors: 1" in result.stdout.splitlines()


def test_explain_interactive_path(make_project):
    root = make_project({"app.py": "x = 1\n"})
    result = run_cli("explain", input=f'"{root}"\n')
    assert result.returncode == 0, result.stderr
    assert "app.py" in result.stdout


def test_explain_invalid_path(tmp_path):
    result = run_cli("explain", tmp_path / "missing")
    assert result.returncode == 2
    assert "does not exist" in result.stderr
    assert "Traceback" not in result.stderr


def test_scan_shorthand_still_works(make_project):
    result = run_cli(make_project({"app.py": "x = 1\n"}), "-q")
    assert result.returncode == 0, result.stderr
    assert "Scanned 1 files" in result.stdout


def test_explain_honors_gitignore_override(make_project):
    root = make_project({".gitignore": "hidden.py\n", "hidden.py": "x = 1\n"})
    hidden = run_cli("explain", root)
    included = run_cli("explain", root, "--no-gitignore")
    assert hidden.returncode == included.returncode == 0
    assert "hidden.py" not in hidden.stdout
    assert "hidden.py" in included.stdout
