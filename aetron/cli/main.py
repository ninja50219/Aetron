"""Command-line entry point for Aetron.

Each pipeline stage gets a subcommand, so the tool stays usable while the
later stages are still missing: "scan" answers what is in the project,
"analyze" answers how it fits together, and "summary" reduces both to what a
newcomer reads first.

The three retrieval levels get a command each - "search", "structure" and
"source" - rather than one command that walks them. That is not a convenience:
the protocol's rule is that escalating to a more expensive level is always a
decision someone makes, and a command that quietly ran all three would hide the
decision it exists to expose. Run by hand they are also the clearest statement
of the contract an ai_provider will drive.

"ask" drives the three levels on a model's behalf. It stayed a separate
command rather than becoming a flag on analyze, so the path that costs money
is always the one you typed, and it prints each request the model made so the
cost of an answer is visible rather than implied.
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from aetron.ai_providers import DEFAULT_PROVIDER, PROVIDERS, ProviderError, get_provider
from aetron.analyzer import analyze
from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.deadcode import Confidence
from aetron.analyzer.symbols import SymbolKind
from aetron.context.search import DEFAULT_LIMIT, search
from aetron.context.source import get_source
from aetron.context.structure import build_structure, render
from aetron.context.summary import build_summary
from aetron.ask import MAX_STEPS, Step, ask
from aetron.scanner import ScanResult, scan
from aetron.scanner.gitignore import AVAILABLE as gitignore_available
from aetron.scanner.paths import InvalidPathError, normalize_path

PREVIEW_LIMIT = 5

COMMANDS = ("scan", "analyze", "summary", "search", "structure", "source", "ask")


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
    # On stderr for the same reason the pathspec note is: progress is about
    # the run, not part of the result, and the result may be piped.
    print(f"\rScanning... {count} files", end="", flush=True, file=sys.stderr)


def clear_progress() -> None:
    print("\r" + " " * 40 + "\r", end="", flush=True, file=sys.stderr)


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

    subcommands.add_parser(
        "summary", parents=[common], help="what a newcomer to this project reads first"
    )

    # The three retrieval levels. Each is its own command because escalating to
    # a more expensive one is meant to be a decision, not a default.
    machine = argparse.ArgumentParser(add_help=False)
    machine.add_argument(
        "--json", action="store_true", help="emit JSON rather than text"
    )

    search_command = subcommands.add_parser(
        "search",
        parents=[common, machine],
        help="level 1: rank the files a question might be about",
    )
    search_command.add_argument("query", help="what to look for, e.g. 'login'")
    search_command.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="how many candidates to return"
    )

    structure_command = subcommands.add_parser(
        "structure",
        parents=[common, machine],
        help="level 2: the shape of one file, without its code",
    )
    structure_command.add_argument("file", help="a path as search reports it")

    source_command = subcommands.add_parser(
        "source",
        parents=[common, machine],
        help="level 3: the code of one definition",
    )
    source_command.add_argument("file", help="a path as search reports it")
    source_command.add_argument("symbol", help="a name as structure reports it")

    ask_command = subcommands.add_parser(
        "ask",
        parents=[common],
        help="ask a model a question, and let it drive the three levels",
    )
    ask_command.add_argument("question", help="e.g. 'where is login?'")
    ask_command.add_argument(
        "--provider",
        choices=PROVIDERS,
        default=DEFAULT_PROVIDER,
        help=f"which model to ask (default: {DEFAULT_PROVIDER}, which runs locally)",
    )
    ask_command.add_argument("--model", help="model name, if not the provider's default")
    ask_command.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help="how many requests the model may make before it has to answer",
    )
    ask_command.add_argument(
        "--quiet", action="store_true", help="the answer only, without the working"
    )

    return parser


def run_scan(args, root: Path) -> ScanResult:
    interactive = sys.stderr.isatty()
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


def command_summary(args, root: Path) -> None:
    scan_result = run_scan(args, root)
    summary = build_summary(scan_result, analyze(scan_result))

    print(f"{summary.name}: {summary.file_count} files, {summary.line_count} lines")
    if summary.primary_language:
        languages = ", ".join(
            f"{name} {count}" for name, count in sorted(
                summary.languages.items(), key=lambda item: -item[1]
            )[:5]
        )
        print(f"Languages: {languages}")

    if summary.symbol_counts:
        counts = ", ".join(f"{kind} {n}" for kind, n in summary.symbol_counts.items())
        print(f"Symbols: {counts}")
        print(f"Import edges: {summary.import_edges}")

    if summary.notable_dependencies:
        print(f"\nDependencies: {', '.join(summary.notable_dependencies)}")

    if summary.key_files:
        print("\nStart reading here:")
        for file_summary in summary.key_files[:PREVIEW_LIMIT]:
            print(
                f"  {file_summary.rel_path:<48} "
                f"{file_summary.dependents} dependents, {file_summary.symbols} symbols"
            )

    if summary.insights:
        print("\nFindings:")
        for insight in summary.insights:
            print(f"  [{insight.severity.value}] {insight.summary}")
            if insight.files:
                print(f"      {', '.join(insight.files[:3])}")

    if summary.unparsed_languages:
        unparsed = ", ".join(
            f"{suffix} x{n}" for suffix, n in summary.unparsed_languages.items()
        )
        print(f"\nNo parser yet: {unparsed}")


def command_search(args, root: Path) -> None:
    """Level 1: rank the files a question might be about."""
    scan_result = run_scan(args, root)
    candidates = search(scan_result, analyze(scan_result), args.query, limit=args.limit)

    if args.json:
        print(json.dumps([
            {
                "rel_path": c.rel_path,
                "score": round(c.score, 4),
                "percent": c.percent,
                "language": c.language,
                "parsed": c.parsed,
                "line": c.best_line,
                "reason": c.reason,
            }
            for c in candidates
        ], indent=2))
        return

    if not candidates:
        print(f"Nothing in this project matches '{args.query}'.")
        return

    print(f"Candidates for '{args.query}':\n")
    for candidate in candidates:
        location = f":{candidate.best_line}" if candidate.best_line else ""
        note = "" if candidate.parsed else "  [no parser for this language]"
        print(f"  {candidate.percent:>3}%  {candidate.rel_path}{location}")
        print(f"        {candidate.reason}{note}")


def command_structure(args, root: Path) -> None:
    """Level 2: the shape of one file, with no code in it."""
    scan_result = run_scan(args, root)
    structure = build_structure(scan_result, analyze(scan_result), args.file)

    if args.json:
        print(json.dumps(structure.to_dict(), indent=2))
        return

    print(render(structure))


def command_source(args, root: Path) -> None:
    """Level 3: the code of one definition."""
    scan_result = run_scan(args, root)
    result = get_source(scan_result, analyze(scan_result), args.file, args.symbol)

    if args.json:
        print(json.dumps({
            "rel_path": result.rel_path,
            "qualified_name": result.qualified_name,
            "kind": result.kind,
            "line": result.line,
            "end_line": result.end_line,
            "start_line": result.start_line,
            "location": result.location,
            "text": result.text,
            "problem": result.problem,
        }, indent=2))
        return

    if result.problem:
        print(f"{result.rel_path}: {result.problem}")
        if not result.text:
            return
        print()

    print(f"{result.kind} {result.qualified_name} - {result.location}\n")
    print(result.numbered())


def print_step(step: Step) -> None:
    """One request the model made, as it happens."""
    if not step.command:
        print("  ...  the model did not issue a command")
        return

    if step.command == "ANSWER":
        return

    marker = "  x  " if step.refused else "  ->  "
    print(f"{marker}{step.command} {step.argument}".rstrip())

    if step.refused:
        print(f"       {step.observation.splitlines()[0]}")


def command_ask(args, root: Path) -> None:
    """Levels 1 to 3, driven by a model rather than by hand."""
    try:
        provider = get_provider(args.provider, args.model)
    except ProviderError as exc:
        print(f"{exc}")
        sys.exit(1)

    scan_result = run_scan(args, root)
    analysis = analyze(scan_result)

    if not args.quiet:
        print(f"Asking {provider.name} ({provider.model}): {args.question}\n")

    answer = ask(
        provider,
        scan_result,
        analysis,
        args.question,
        max_steps=args.max_steps,
        on_step=None if args.quiet else print_step,
    )

    if answer.incomplete:
        print(f"\n{answer.incomplete}")
        sys.exit(1)

    print(f"\n{answer.text}")

    if not args.quiet:
        # What the answer actually cost: the levels are only worth having if
        # this stays short, so it is reported rather than left to be assumed.
        requests = len([s for s in answer.steps if s.command and s.command != "ANSWER"])
        read = ", ".join(answer.files_read) or "no source code"
        print(f"\n({requests} requests; source read from: {read})")


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
        # stderr, not stdout: this is a note about the tool, not part of the
        # answer. On stdout it was concatenated with --json output, so a
        # machine consumer got invalid JSON purely because an optional
        # dependency was absent.
        print(
            "Note: pathspec is not installed, .gitignore files are not applied.\n",
            file=sys.stderr,
        )

    root = resolve_root(parser, args.path)

    handlers = {
        "analyze": command_analyze,
        "summary": command_summary,
        "search": command_search,
        "structure": command_structure,
        "source": command_source,
        "ask": command_ask,
    }
    handlers.get(args.command, command_scan)(args, root)


if __name__ == "__main__":
    main()
