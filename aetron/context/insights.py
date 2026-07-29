"""Structural findings derived from the import graph and symbol index.

Everything here is deterministic: no model is involved, and none is needed.
Circular imports, orphan modules and over-central files are properties of the
graph, so they are computed once and stated as fact. A language model is only
useful later, for turning these facts into prose - and prose it invents on top
of facts is far cheaper to trust than facts it invents on its own.
"""

from dataclasses import dataclass
from enum import Enum

from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.symbols import SymbolKind


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Insight:
    """One structural observation about the project."""

    kind: str
    severity: Severity
    summary: str
    # Files the observation is about, most relevant first.
    files: list[str]
    detail: str = ""


# A file imported by this many others is a hub: changing it is expensive, and
# a newcomer should read it early. Chosen to be roughly "a tenth of a
# medium project" rather than from any theory.
HUB_DEPENDENT_RATIO = 0.10
HUB_MINIMUM_DEPENDENTS = 3

# Below this, "most depended on" says more about project size than design.
MIN_FILES_FOR_HUBS = 5


def find_insights(analysis: AnalysisResult) -> list[Insight]:
    """Everything structural worth telling a developer about the project."""
    insights: list[Insight] = []

    insights.extend(_circular_imports(analysis))
    insights.extend(_orphan_modules(analysis))
    insights.extend(_hub_files(analysis))

    severity_order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    insights.sort(key=lambda i: (severity_order[i.severity], i.kind))
    return insights


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every group of files that import each other, directly or indirectly.

    Tarjan's algorithm, written iteratively: a deep import chain would blow
    the recursion limit on a large project, and this runs on whole codebases.
    """
    index_of: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[list[str]] = []
    counter = 0

    for start in sorted(graph):
        if start in index_of:
            continue

        # Each frame is (node, iterator over its successors).
        work: list[tuple[str, list[str]]] = [(start, sorted(graph.get(start, ())))]
        index_of[start] = lowlink[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)

        while work:
            node, successors = work[-1]

            if successors:
                target = successors.pop()
                if target not in index_of:
                    index_of[target] = lowlink[target] = counter
                    counter += 1
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, sorted(graph.get(target, ()))))
                elif target in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[target])
                continue

            work.pop()

            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])

            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                # A single file is only a cycle if it imports itself, which
                # the analyzer already filters out.
                if len(component) > 1:
                    components.append(sorted(component))

    components.sort(key=lambda c: (-len(c), c[0]))
    return components


def _circular_imports(analysis: AnalysisResult) -> list[Insight]:
    findings = []

    for cycle in find_cycles(analysis.imports):
        findings.append(
            Insight(
                kind="circular-import",
                # Python tolerates many import cycles at runtime, but they
                # make a codebase impossible to read in any order, so this is
                # a design problem rather than a bug.
                severity=Severity.MEDIUM if len(cycle) == 2 else Severity.HIGH,
                summary=f"{len(cycle)} files import each other in a cycle",
                files=cycle,
                detail=" -> ".join(cycle) + f" -> {cycle[0]}",
            )
        )

    return findings


def _orphan_modules(analysis: AnalysisResult) -> list[Insight]:
    """Files that neither import anything local nor are imported."""
    orphans = sorted(
        f.rel_path
        for f in analysis.files
        if not analysis.imports.get(f.rel_path)
        and not analysis.imported_by.get(f.rel_path)
        and f.symbols  # an empty __init__.py is not an orphan, it is a marker
    )

    if not orphans:
        return []

    return [
        Insight(
            kind="orphan-module",
            severity=Severity.LOW,
            summary=f"{len(orphans)} files are not connected to the rest of the project",
            files=orphans,
            detail=(
                "Nothing imports these and they import nothing local. They may be "
                "scripts, entry points, or leftovers."
            ),
        )
    ]


def _hub_files(analysis: AnalysisResult) -> list[Insight]:
    """Files that a large share of the project depends on."""
    total = len(analysis.files)
    if total < MIN_FILES_FOR_HUBS:
        return []

    threshold = max(HUB_MINIMUM_DEPENDENTS, int(total * HUB_DEPENDENT_RATIO))
    hubs = sorted(
        ((len(sources), path) for path, sources in analysis.imported_by.items() if len(sources) >= threshold),
        reverse=True,
    )

    if not hubs:
        return []

    return [
        Insight(
            kind="hub-file",
            severity=Severity.LOW,
            summary=f"{len(hubs)} files are depended on by much of the project",
            files=[path for _, path in hubs],
            detail=", ".join(f"{path} ({count} dependents)" for count, path in hubs[:5]),
        )
    ]


# TODO: an "unused public API" insight, once __all__ contents are available.
# A first attempt flagged every module-level name not used by another file,
# which reported 60 findings on this project - all of them ordinary helpers in
# a CLI module. Python does not require a leading underscore for module-private
# names, so "public and unused" is only meaningful for names the project
# explicitly exports. Recommendation: have the parser record __all__ list
# contents and re-exports in __init__.py, then report only those, which turns a
# noisy heuristic into a precise statement.
