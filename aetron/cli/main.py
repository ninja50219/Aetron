"""Command-line entry point for Aetron.

Each pipeline stage gets a subcommand, so the tool stays usable while the
later stages are still missing: "scan" answers what is in the project,
"analyze" answers how it fits together.

TODO: once ai_providers lands, add an "ask" subcommand taking a question and a
model. Recommendation: keep it a separate command rather than a flag on
analyze, so the expensive path is always explicit.
"""

import argparse
import sys
from collections import Counter
from pathlib import Path

from aetron.analyzer import analyze
from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.deadcode import Confidence
from aetron.analyzer.symbols import SymbolKind
from aetron.scanner import ScanResult, scan
from aetron.scanner.gitignore import AVAILABLE as gitignore_available
from aetron.scanner.paths import InvalidPathError, normalize_path

PREVIEW_LIMIT = 5

COMMANDS = ("scan", "analyze")


def prompt_for_path() -> Path:
    """Ask for a project directory until a usable one is given."""
    print("Aetron - project scanner")
    print("Enter a project directory (blank line or Ctrl+C to quit).\n")

    while True:
        try:
            raw = input("path> ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if not raw.strip():
            sys.exit(0)

        try:
            return normalize_path(raw)
        except InvalidPathError as exc:
            print(f"  {exc}\n")


def print_progress(count: int, rel_path: str) -> None:
    # \r keeps the counter on one line, which only makes sense on a terminal.
    print(f"\rScanning... {count} files", end="", flush=True)


def clear_progress() -> None:
    print("\r" + " " * 40 + "\r", end="", flush=True)


def print_scan_report(result: ScanResult, show_files: bool, listing_pruned: bool) -> None:
    if show_files:
        for f in result.files:
            print(f"{f.rel_path:<60} {f.language:<12} {f.lines:>7} lines")
        print()

    print(f"Scanned {len(result.files)} files, {result.total_lines} lines")
    for language, count in Counter(f.language for f in result.files).most_common():
        print(f"  {language}: {count}")

    if result.docs:
        print(f"\nDocumentation: {len(result.docs)} files")
        for d in result.docs[:PREVIEW_LIMIT]:
            print(f"  {d.rel_path}")
        if len(result.docs) > PREVIEW_LIMIT:
            print(f"  ... and {len(result.docs) - PREVIEW_LIMIT} more")

    if result.dependencies:
        print(f"\nDependencies: {len(result.dependencies)}")
        for ecosystem, count in Counter(
            d.ecosystem for d in result.dependencies
        ).most_common():
            print(f"  {ecosystem}: {count}")

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} files:")
        for reason, count in Counter(s.reason for s in result.skipped).most_common():
            print(f"  {reason}: {count}")

    if result.pruned_dirs and not listing_pruned:
        print(f"\nPruned {len(result.pruned_dirs)} directories (--show-pruned to list)")


def print_analysis_report(result: AnalysisResult) -> None:
    print(f"\nParsed {len(result.files)} files")
    for kind in SymbolKind:
        count = result.count_of(kind)
        if count:
            print(f"  {kind.value}: {count}")

    if result.unparsed:
        # Being explicit about this beats letting a user assume full coverage.
        languages = Counter(Path(p).suffix for p in result.unparsed)
        summary = ", ".join(f"{suffix} x{n}" for suffix, n in languages.most_common(5))
        print(f"\nNo parser yet for {len(result.unparsed)} files: {summary}")

    if result.parse_errors:
        print(f"\nParse errors: {len(result.parse_errors)}")
        for rel_path, message in result.parse_errors[:PREVIEW_LIMIT]:
            print(f"  {rel_path}: {message}")

    edges = sum(len(targets) for targets in result.imports.values())
    print(f"\nImport graph: {edges} edges between project files")

    most_used = sorted(
        ((len(sources), path) for path, sources in result.imported_by.items() if sources),
        reverse=True,
    )[:PREVIEW_LIMIT]
    if most_used:
        print("  most depended on:")
        for count, path in most_used:
            print(f"    {path} <- {count} files")

    entry_points = result.entry_points()
    if entry_points:
        print(f"\nEntry points ({len(entry_points)} files nothing imports):")
        for path in entry_points[:PREVIEW_LIMIT]:
            print(f"  {path}")
        if len(entry_points) > PREVIEW_LIMIT:
            print(f"  ... and {len(entry_points) - PREVIEW_LIMIT} more")


def print_dead_code(result: AnalysisResult, minimum: Confidence) -> None:
    order = [Confidence.HIGH, Confidence.MEDIUM, Confidence.LOW]
    allowed = set(order[: order.index(minimum) + 1])

    found = [c for c in result.dead_code() if c.confidence in allowed]
    if not found:
        print("\nNo unused definitions found.")
        return

    print(f"\nPossibly unused ({len(found)}, confidence >= {minimum.value}):")
    for candidate in found:
        location = f"{candidate.rel_path}:{candidate.symbol.line}"
        print(
            f"  {candidate.confidence.value:<7} {location:<55} "
            f"{candidate.symbol.qualified_name} - {candidate.reason}"
        )


def resolve_root(parser: argparse.ArgumentParser, raw: str | None) -> Path:
    if raw is None:
        return prompt_for_path()
    try:
        return normalize_path(raw)
    except InvalidPathError as exc:
        parser.error(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aetron")
    subcommands = parser.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("path", nargs="?", help="project directory; omit to be asked")
    common.add_argument(
        "--no-gitignore", action="store_true", help="ignore the project's .gitignore"
    )

    scan_command = subcommands.add_parser(
        "scan", parents=[common], help="list the files a project contains"
    )
    scan_command.add_argument("-q", "--quiet", action="store_true", help="summary only")
    scan_command.add_argument(
        "--show-skipped", action="store_true", help="list every skipped file"
    )
    scan_command.add_argument(
        "--show-pruned", action="store_true", help="list every ignored directory"
    )
    scan_command.add_argument(
        "--show-deps", action="store_true", help="list every dependency with its version"
    )

    analyze_command = subcommands.add_parser(
        "analyze", parents=[common], help="build the symbol index and import graph"
    )
    analyze_command.add_argument(
        "--dead-code", action="store_true", help="report definitions nothing uses"
    )
    analyze_command.add_argument(
        "--confidence",
        choices=[c.value for c in Confidence],
        default=Confidence.MEDIUM.value,
        help="lowest confidence to report for --dead-code (default: medium)",
    )
    analyze_command.add_argument(
        "--symbol", help="show every definition of a name and where it lives"
    )

    return parser


def run_scan(args, root: Path) -> ScanResult:
    interactive = sys.stdout.isatty()
    result = scan(
        root,
        on_progress=print_progress if interactive else None,
        use_gitignore=not args.no_gitignore,
    )
    if interactive:
        clear_progress()
    return result


def command_scan(args, root: Path) -> None:
    result = run_scan(args, root)
    print_scan_report(result, not args.quiet, args.show_pruned)

    if args.show_skipped and result.skipped:
        print("\nSkipped files:")
        for s in result.skipped:
            print(f"  {s.rel_path:<60} {s.reason}")

    if args.show_deps and result.dependencies:
        print("\nDependencies:")
        for d in result.dependencies:
            print(f"  {d.ecosystem:<8} {d.name:<40} {d.version:<20} {d.manifest}")

    if args.show_pruned and result.pruned_dirs:
        print("\nPruned directories:")
        for path in result.pruned_dirs:
            print(f"  {path}")


def command_analyze(args, root: Path) -> None:
    scan_result = run_scan(args, root)
    print(f"Scanned {len(scan_result.files)} files, {scan_result.total_lines} lines")

    result = analyze(scan_result)

    if args.symbol:
        found = result.find(args.symbol)
        if not found:
            print(f"\nNo definition of '{args.symbol}' found.")
            return
        print(f"\n'{args.symbol}' is defined {len(found)} time(s):")
        for rel_path, symbol in found:
            signature = f"({', '.join(symbol.parameters)})" if symbol.is_callable else ""
            print(f"  {rel_path}:{symbol.line} {symbol.kind.value} {symbol.qualified_name}{signature}")
        return

    print_analysis_report(result)

    if args.dead_code:
        print_dead_code(result, Confidence(args.confidence))


def main() -> None:
    parser = build_parser()

    # "aetron ." and "aetron -q ." keep working: scanning is the default, and
    # argparse would reject the path as an unknown subcommand before any
    # fallback could run, so the argument list is fixed up first.
    argv = sys.argv[1:]
    if (not argv or argv[0] not in COMMANDS) and argv[:1] != ["-h"] and argv[:1] != ["--help"]:
        argv = ["scan", *argv]

    args = parser.parse_args(argv)

    if not args.no_gitignore and not gitignore_available:
        print("Note: pathspec is not installed, .gitignore files are not applied.\n")

    root = resolve_root(parser, args.path)

    if args.command == "analyze":
        command_analyze(args, root)
    else:
        command_scan(args, root)


if __name__ == "__main__":
    main()
