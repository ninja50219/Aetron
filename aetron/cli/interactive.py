"""Keep navigation in the terminal so readers need not remember paths.

This is another consumer of the retrieval API, not a wrapper around commands:
one scan and analysis belong to the selected project until the reader refreshes
them. Numbered choices carry the exact identifiers between retrieval levels.
History lives outside the repository so switching projects preserves it.
"""

import json
import os
from pathlib import Path
import sys
import tempfile

from aetron.analyzer import analyze
from aetron.context import build_structure, build_summary, get_source, search
from aetron.context.render import render_summary
from aetron.context.structure import render
from aetron.scanner import scan
from aetron.scanner.gitignore import AVAILABLE as gitignore_available
from aetron.scanner.paths import InvalidPathError, normalize_path


def load_history(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(p, str) for p in data):
            raise ValueError("expected a list of project paths")
        return list(dict.fromkeys(data))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        print(f"Cannot read recent projects: {exc}", file=sys.stderr)
        return []


def save_history(path: Path, history: list[str]) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # An interrupted write must not destroy the paths already remembered.
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(history, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except OSError as exc:
        print(f"Cannot save recent projects: {exc}", file=sys.stderr)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def choose(labels: list[str], prompt: str = "choice> ") -> int:
    for number, label in enumerate(labels, 1):
        print(f"  {number}. {label}")
    print("  0. Back / quit")
    while True:
        raw = input(prompt).strip()
        try:
            number = int(raw)
        except ValueError:
            number = -1
        if 0 <= number <= len(labels):
            return number
        print(f"Enter a number from 0 to {len(labels)}.")


def select_project(history: list[str], history_path: Path) -> Path | None:
    while True:
        print("\nRecent projects")
        selected = choose([*history, "Open a new project"])
        if selected == 0:
            return None
        raw = (
            input("Project directory (0 to go back)> ")
            if selected == len(history) + 1 else history[selected - 1]
        )
        if raw.strip() == "0":
            continue
        try:
            root = normalize_path(raw)
        except (InvalidPathError, OSError, ValueError, RuntimeError) as exc:
            print(f"Cannot open project: {exc}")
            continue
        resolved = str(root)
        history[:] = [resolved, *(p for p in history if p != resolved)]
        save_history(history_path, history)
        return root


def find_code(scanned, analysis) -> None:
    query = input("What would you like to find? (0 to go back)> ").strip()
    if query == "0":
        return
    if not query:
        print("Enter a word or a question to search for.")
        return
    candidates = search(scanned, analysis, query, limit=max(1, len(scanned.files)))
    if not candidates:
        print(f"Nothing in this project matches '{query}'.")
        return
    while True:
        print("\nMatching files (percentages rank matches, not probabilities)")
        selected = choose([
            f"{c.rel_path} - {c.percent}% - {c.reason}"
            + (" [no parser for this language]" if not c.parsed else "")
            for c in candidates
        ])
        if selected == 0:
            return
        structure = build_structure(scanned, analysis, candidates[selected - 1].rel_path)
        print("\n" + render(structure))
        if not structure.available or not structure.symbols:
            continue
        while True:
            print("\nChoose a definition to read its code")
            selected_symbol = choose([
                f"{s.kind} {s.qualified_name} (lines {s.line}-{s.end_line})"
                for s in structure.symbols
            ])
            if selected_symbol == 0:
                break
            symbol = structure.symbols[selected_symbol - 1]
            # The public API accepts names, not line numbers. Overloads cannot
            # be selected individually without returning the wrong definition.
            if sum(s.qualified_name == symbol.qualified_name for s in structure.symbols) > 1:
                print("This name has multiple definitions. The source API cannot select "
                      "one by line number; no source was retrieved.")
                continue
            source = get_source(scanned, analysis, structure.rel_path, symbol.qualified_name)
            if source.problem:
                print(f"{source.rel_path}: {source.problem}")
                print("Rescan the project if files have changed.")
            if source.text:
                print(f"\n{source.kind} {source.qualified_name} - {source.location}")
                print(source.numbered())


def project_session(root: Path) -> None:
    def refresh():
        print(f"\nLoading {root} ...")
        result = scan(root, use_gitignore=True)
        index = analyze(result)
        print(f"Loaded {len(result.files)} files, {result.total_lines} lines.")
        print(f"Skipped {len(result.skipped)} files; pruned {len(result.pruned_dirs)} directories.")
        for directory in result.pruned_dirs:
            print(f"  Pruned: {directory}")
        print("Use 'Review omitted files' for every skipped file and its reason.")
        return result, index

    try:
        scanned, analysis = refresh()
    except (OSError, ValueError) as exc:
        print(f"Cannot load project: {exc}")
        return
    while True:
        print(f"\nProject: {root}")
        selected = choose([
            "Explore the project", "Find something", "Review omitted files",
            "Rescan the project", "Choose another project",
        ])
        try:
            if selected in (0, 5):
                return
            if selected == 1:
                print(render_summary(build_summary(scanned, analysis)))
            elif selected == 2:
                find_code(scanned, analysis)
            elif selected == 3:
                for skipped in scanned.skipped:
                    print(f"{skipped.rel_path}: {skipped.reason}")
                for directory in scanned.pruned_dirs:
                    print(f"Pruned: {directory}")
                if not scanned.skipped and not scanned.pruned_dirs:
                    print("No files or directories were omitted.")
            elif selected == 4:
                scanned, analysis = refresh()
        except (OSError, ValueError) as exc:
            print(f"Could not complete that action: {exc}")


def run(history_path: Path | None = None) -> None:
    """Own the terminal lifetime so cancellation works at every prompt."""
    print("Aetron - explore a project by choosing numbers")
    if not gitignore_available:
        print("Note: pathspec is not installed, .gitignore files are not applied.",
              file=sys.stderr)
    try:
        if history_path is None:
            history_path = Path.home() / ".aetron" / "recent-projects.json"
        history = load_history(history_path)
        while True:
            root = select_project(history, history_path)
            if root is None:
                break
            project_session(root)
    except (EOFError, KeyboardInterrupt):
        print("\nGoodbye.")
