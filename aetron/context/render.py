"""Plain-text rendering of a project summary.

The renderer reads the summary and nothing else. It reports what scanning and
analysis recorded and draws no conclusions of its own, so the text never claims
more than the data supports.
"""

from .summary import TOP_N, ProjectSummary


def render_summary(summary: ProjectSummary) -> str:
    """Render a summary as an English plain-text report."""
    lines = [f"Project: {summary.name}", f"Root: {summary.root}"]
    lines += _section(
        "Size",
        [
            f"Files: {summary.file_count}",
            f"Lines: {summary.line_count}",
            f"Primary language: {summary.primary_language or 'none'}",
        ],
    )
    lines += _section("Languages (files per language)", _counts(summary.languages))
    lines += _section("Symbols", _counts(summary.symbol_counts))
    lines += _section("Imports", [f"Import edges: {summary.import_edges}"])
    lines += _section(
        "Modules with no incoming local imports (not confirmed runtime entrypoints)",
        summary.entry_points,
    )
    lines += _section(
        "Key files (ranked by dependents, then symbols)",
        [
            f"{f.rel_path} ({f.language}, {f.lines} lines, {f.symbols} symbols, "
            f"imported by {f.dependents}, imports {f.dependencies})"
            for f in summary.key_files
        ],
    )
    lines += _section("Declared dependency entries by ecosystem", _counts(summary.ecosystems))
    lines += _section(
        "Declared dependencies", [name.strip() for name in summary.notable_dependencies]
    )
    lines += _section("Documentation", summary.documentation)
    lines += _section("Insights", _insights(summary))
    lines += _section("Limitations", _limitations(summary))
    return "\n".join(lines) + "\n"


def _section(title: str, items: list[str]) -> list[str]:
    return ["", title] + [f"  {item}" for item in items or ["none"]]


def _counts(counts: dict[str, int]) -> list[str]:
    """Largest first, ties by name, so the output is stable."""
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [f"{name}: {count}" for name, count in ordered]


def _insights(summary: ProjectSummary) -> list[str]:
    lines: list[str] = []
    for insight in summary.insights:
        lines.append(f"[{insight.severity.value}] {insight.kind}: {insight.summary}")
        lines += [f"  {text}" for text in insight.detail.splitlines()]
        lines += [f"  File: {path}" for path in insight.files]
    return lines


def _limitations(summary: ProjectSummary) -> list[str]:
    unsupported = ", ".join(_counts(summary.unparsed_languages)) or "none"
    lines = [
        f"Unsupported file types (not parsed): {unsupported}",
        f"Parse errors: {len(summary.parse_errors)}",
    ]
    lines += [f"  {path}: {message}" for path, message in summary.parse_errors]
    if summary.parse_errors:
        lines.append(
            "Files with parse errors contribute no imports or symbols and are "
            "excluded from key files and reading candidates; import edges, "
            "dependent counts and the no-incoming-imports list may be incomplete."
        )
    lines += [
        f"Skipped files: {summary.skipped_file_count}",
        f"Pruned directories: {summary.pruned_directory_count}",
        f"Module, key file, dependency and documentation lists keep up to {TOP_N} items each.",
        "Findings reflect static analysis of scanned files and may not cover the whole project.",
    ]
    return lines
