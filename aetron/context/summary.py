"""One structure describing a whole project.

This is the handover point between analysis and everything that consumes it: a
renderer, a language model, a GUI. Building it is deliberately separate from
rendering it, so that adding a new output format never means touching analysis
again.

Everything here is derived, never re-derived: the summary holds no logic of its
own beyond selection and ranking.
"""

from dataclasses import dataclass, field
from pathlib import Path

from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.symbols import SymbolKind
from aetron.scanner.scanner import ScanResult

from .insights import Insight, find_insights

# How many items of each kind a summary keeps. A summary that lists everything
# is just the raw data again, and the point is to be smaller than the project.
TOP_N = 10


@dataclass
class FileSummary:
    """A file worth mentioning, and why."""

    rel_path: str
    language: str
    lines: int
    symbols: int
    dependents: int
    dependencies: int


@dataclass
class ProjectSummary:
    root: Path
    name: str

    # Size
    file_count: int = 0
    line_count: int = 0
    languages: dict[str, int] = field(default_factory=dict)

    # Structure
    symbol_counts: dict[str, int] = field(default_factory=dict)
    import_edges: int = 0
    entry_points: list[str] = field(default_factory=list)
    key_files: list[FileSummary] = field(default_factory=list)

    # Context
    ecosystems: dict[str, int] = field(default_factory=dict)
    notable_dependencies: list[str] = field(default_factory=list)
    documentation: list[str] = field(default_factory=list)

    # Findings
    insights: list[Insight] = field(default_factory=list)
    unparsed_languages: dict[str, int] = field(default_factory=dict)

    @property
    def primary_language(self) -> str | None:
        return max(self.languages, key=self.languages.get, default=None)


def build_summary(scan_result: ScanResult, analysis: AnalysisResult) -> ProjectSummary:
    """Reduce a full scan and analysis to what a reader needs first."""
    summary = ProjectSummary(
        root=scan_result.root,
        name=scan_result.root.name,
        file_count=len(scan_result.files),
        line_count=scan_result.total_lines,
    )

    for file_info in scan_result.files:
        summary.languages[file_info.language] = summary.languages.get(file_info.language, 0) + 1

    for kind in SymbolKind:
        count = analysis.count_of(kind)
        if count:
            summary.symbol_counts[kind.value] = count

    summary.import_edges = sum(len(targets) for targets in analysis.imports.values())
    summary.entry_points = analysis.entry_points()[:TOP_N]
    summary.key_files = _rank_files(scan_result, analysis)

    for dependency in scan_result.dependencies:
        summary.ecosystems[dependency.ecosystem] = (
            summary.ecosystems.get(dependency.ecosystem, 0) + 1
        )
    summary.notable_dependencies = _notable_dependencies(scan_result)

    summary.documentation = [doc.rel_path for doc in scan_result.docs][:TOP_N]
    summary.insights = find_insights(analysis)

    for rel_path in analysis.unparsed:
        suffix = Path(rel_path).suffix or "(none)"
        summary.unparsed_languages[suffix] = summary.unparsed_languages.get(suffix, 0) + 1

    return summary


def _rank_files(scan_result: ScanResult, analysis: AnalysisResult) -> list[FileSummary]:
    """The files a newcomer should read first.

    Ranked by how many other files depend on them, then by how much structure
    they hold. Size alone is a poor guide: the longest file in a project is
    often a table of constants nobody needs to read.
    """
    lines_by_path = {f.rel_path: f.lines for f in scan_result.files}
    symbols_by_path = {f.rel_path: len(f.symbols) for f in analysis.files}

    ranked = []
    for file_symbols in analysis.files:
        rel_path = file_symbols.rel_path
        ranked.append(
            FileSummary(
                rel_path=rel_path,
                language=file_symbols.language,
                lines=lines_by_path.get(rel_path, 0),
                symbols=symbols_by_path.get(rel_path, 0),
                dependents=len(analysis.imported_by.get(rel_path, ())),
                dependencies=len(analysis.imports.get(rel_path, ())),
            )
        )

    ranked.sort(key=lambda f: (f.dependents, f.symbols), reverse=True)
    return ranked[:TOP_N]


def _notable_dependencies(scan_result: ScanResult) -> list[str]:
    """Third-party packages, deduplicated across manifests.

    A monorepo declares the same framework in several manifests; naming it once
    is what a reader wants.
    """
    seen: dict[str, str] = {}

    for dependency in scan_result.dependencies:
        seen.setdefault(dependency.name, dependency.version)

    return [f"{name} {version}" for name, version in list(seen.items())[:TOP_N]]
