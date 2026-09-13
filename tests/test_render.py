"""Exercise the report with the same summary types used by the pipeline."""

from pathlib import Path

from aetron.context.insights import Insight, Severity
from aetron.context.render import render_summary
from aetron.context.summary import TOP_N, FileSummary, ProjectSummary

HEADINGS = [
    "Size",
    "Languages (files per language)",
    "Symbols",
    "Imports",
    "Modules with no incoming local imports (not confirmed runtime entrypoints)",
    "Key files (ranked by dependents, then symbols)",
    "Declared dependency entries by ecosystem",
    "Declared dependencies",
    "Documentation",
    "Insights",
    "Limitations",
]


def _full_summary() -> ProjectSummary:
    return ProjectSummary(
        root=Path("demo"),
        name="demo",
        file_count=3,
        line_count=120,
        languages={"markdown": 1, "python": 2},
        symbol_counts={"class": 1, "function": 5},
        import_edges=2,
        entry_points=["demo/cli.py"],
        key_files=[
            FileSummary(
                rel_path="demo/core.py",
                language="python",
                lines=80,
                symbols=4,
                dependents=2,
                dependencies=0,
            )
        ],
        ecosystems={"pypi": 2},
        notable_dependencies=["click 8.1", "rich "],
        documentation=["README.md"],
        insights=[
            Insight(
                kind="cycle",
                severity=Severity.MEDIUM,
                summary="Import cycle",
                files=["demo/a.py", "demo/b.py"],
                detail="a imports b\nb imports a",
            )
        ],
        unparsed_languages={".js": 2},
        parse_errors=[("demo/broken.py", "invalid syntax")],
        skipped_file_count=4,
        pruned_directory_count=1,
    )


def test_empty_project_renders_every_section():
    lines = render_summary(ProjectSummary(root=Path("empty"), name="empty")).splitlines()

    for heading in HEADINGS:
        assert heading in lines
    assert "  Files: 0" in lines
    assert "  Primary language: none" in lines
    assert "  Unsupported file types (not parsed): none" in lines
    assert "  Parse errors: 0" in lines
    assert "  none" in lines


def test_full_summary_reports_recorded_data():
    lines = render_summary(_full_summary()).splitlines()

    for expected in [
        "Project: demo",
        "  Lines: 120",
        "  Primary language: python",
        "  function: 5",
        "  Import edges: 2",
        "  demo/cli.py",
        "  demo/core.py (python, 80 lines, 4 symbols, imported by 2, imports 0)",
        "  pypi: 2",
        "  click 8.1",
        "  rich",
        "  README.md",
        "  [medium] cycle: Import cycle",
        "    b imports a",
        "    File: demo/b.py",
        "  Unsupported file types (not parsed): .js: 2",
        "    demo/broken.py: invalid syntax",
        "  Skipped files: 4",
        "  Pruned directories: 1",
    ]:
        assert expected in lines


def test_counts_are_largest_first():
    text = render_summary(_full_summary())

    assert text.index("python: 2") < text.index("markdown: 1")
    assert text.index("function: 5") < text.index("class: 1")


def test_entry_points_are_not_presented_as_runtime_entrypoints():
    text = render_summary(_full_summary())

    assert "not confirmed runtime entrypoints" in text
    assert "Entry points" not in text


def test_states_list_limit_and_coverage():
    text = render_summary(ProjectSummary(root=Path("empty"), name="empty"))

    assert f"keep up to {TOP_N} items each" in text
    assert "may not cover the whole project" in text
    assert "Files with parse errors contribute no imports" not in text


def test_parse_failures_explain_effect_on_import_counts():
    text = render_summary(_full_summary())
    assert "Files with parse errors contribute no imports or symbols" in text
    assert "no-incoming-imports list may be incomplete" in text


def test_insight_without_files_or_detail_is_one_line():
    summary = ProjectSummary(root=Path("demo"), name="demo")
    summary.insights = [Insight("hub", Severity.MEDIUM, "Many dependents", [])]

    lines = render_summary(summary).splitlines()

    index = lines.index("  [medium] hub: Many dependents")
    assert lines[index + 1] == ""


def test_no_trailing_whitespace_and_single_final_newline():
    text = render_summary(_full_summary())

    assert text.endswith("\n") and not text.endswith("\n\n")
    assert all(line == line.rstrip() for line in text.splitlines())
