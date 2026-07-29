"""Command-line entry point for Aetron."""

import argparse
import sys
from collections import Counter
from pathlib import Path

from aetron.scanner import ScanResult, scan
from aetron.scanner.gitignore import AVAILABLE as gitignore_available
from aetron.scanner.paths import InvalidPathError, normalize_path


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
    # Redirected to a file it would be noise, so it is disabled there.
    print(f"\rScanning... {count} files", end="", flush=True)


def clear_progress() -> None:
    print("\r" + " " * 40 + "\r", end="", flush=True)


def print_report(result: ScanResult, show_files: bool, listing_pruned: bool = False) -> None:
    if show_files:
        for f in result.files:
            print(f"{f.rel_path:<60} {f.language:<12} {f.lines:>7} lines")
        print()

    print(f"Scanned {len(result.files)} files, {result.total_lines} lines")
    for language, count in Counter(f.language for f in result.files).most_common():
        print(f"  {language}: {count}")

    if result.docs:
        print(f"\nDocumentation: {len(result.docs)} files")
        for d in result.docs[:5]:
            print(f"  {d.rel_path}")
        if len(result.docs) > 5:
            print(f"  ... and {len(result.docs) - 5} more")

    if result.dependencies:
        by_ecosystem = Counter(d.ecosystem for d in result.dependencies)
        print(f"\nDependencies: {len(result.dependencies)}")
        for ecosystem, count in by_ecosystem.most_common():
            print(f"  {ecosystem}: {count}")

    if result.skipped:
        print(f"\nSkipped {len(result.skipped)} files:")
        for reason, count in Counter(s.reason for s in result.skipped).most_common():
            print(f"  {reason}: {count}")

    if result.pruned_dirs and not listing_pruned:
        print(f"\nPruned {len(result.pruned_dirs)} directories (--show-pruned to list)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="aetron")
    parser.add_argument(
        "path",
        nargs="?",
        help="project directory to scan; omit to be asked for one",
    )
    parser.add_argument(
        "-q", "--quiet", action="store_true", help="print only the summary"
    )
    parser.add_argument(
        "--show-skipped", action="store_true", help="list every skipped file"
    )
    parser.add_argument(
        "--show-pruned", action="store_true", help="list every ignored directory"
    )
    parser.add_argument(
        "--show-deps", action="store_true", help="list every dependency with its version"
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help="ignore the project's .gitignore files",
    )
    args = parser.parse_args()

    if args.path is None:
        root = prompt_for_path()
    else:
        try:
            root = normalize_path(args.path)
        except InvalidPathError as exc:
            parser.error(str(exc))

    interactive = sys.stdout.isatty()
    if args.no_gitignore is False and not gitignore_available:
        print("Note: pathspec is not installed, .gitignore files are not applied.\n")

    result = scan(
        root,
        on_progress=print_progress if interactive else None,
        use_gitignore=not args.no_gitignore,
    )
    if interactive:
        clear_progress()

    print_report(result, show_files=not args.quiet, listing_pruned=args.show_pruned)

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
        for d in result.pruned_dirs:
            print(f"  {d}")


if __name__ == "__main__":
    main()
