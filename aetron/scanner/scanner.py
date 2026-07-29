"""Project scanner: turns a directory into a list of source files."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .detect import HARD_SIZE_LIMIT, generated_reason
from .docs import is_doc_file
from .gitignore import AVAILABLE as gitignore_available
from .gitignore import GITIGNORE_NAME, GitignoreStack
from .ignore import is_ignored_dir, is_project_root
from .languages import detect_language
from .manifests import Dependency, is_manifest, parse_manifest
from .paths import normalize_path

# Called with (files_seen, current_relative_path) as the walk proceeds.
# A CLI can print a counter, a GUI can drive a progress bar from another thread.
ProgressCallback = Callable[[int, str], None]


@dataclass
class FileInfo:
    """A source file accepted by the scanner.

    ``rel_path`` always uses forward slashes, on every platform, so it can be
    a stable key in a graph, a test and a serialised result alike. ``path`` is
    the native path to use for actual file access.
    """

    path: Path
    rel_path: str
    language: str
    size: int
    lines: int


@dataclass
class SkippedFile:
    """A source file that was recognised but left out, with the reason why."""

    rel_path: str
    reason: str


@dataclass
class DocFile:
    """A documentation file: read for meaning, never parsed as code."""

    rel_path: str
    size: int
    lines: int


@dataclass
class ScanResult:
    root: Path
    files: list[FileInfo] = field(default_factory=list)
    docs: list[DocFile] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    # Directories that were never walked into. Recorded so a large drop in the
    # file count can always be explained, and a wrong rule can be spotted.
    pruned_dirs: list[str] = field(default_factory=list)

    @property
    def total_lines(self) -> int:
        return sum(f.lines for f in self.files)


def _nearest_project_root(rel_dir: str, project_roots: set[str]) -> str:
    """Return the deepest known project root containing ``rel_dir``."""
    if rel_dir in project_roots:
        return rel_dir

    parts = rel_dir.split("/") if rel_dir else []
    for depth in range(len(parts), 0, -1):
        candidate = "/".join(parts[:depth])
        if candidate in project_roots:
            return candidate
    return ""


def _strip_base(rel_path: str, base: str) -> str:
    """Re-express a scan-root-relative path relative to a project root."""
    if not base:
        return rel_path
    return rel_path[len(base) + 1 :] if rel_path.startswith(f"{base}/") else rel_path


def scan(
    root: str | Path,
    on_progress: ProgressCallback | None = None,
    use_gitignore: bool = True,
) -> ScanResult:
    """Walk a project directory and collect its source files.

    Files are never dropped silently: anything recognised as source but left
    out ends up in ``result.skipped`` with a reason.

    ``on_progress`` is optional and lets a front end report activity without
    this function knowing anything about the front end.

    ``use_gitignore`` applies the .gitignore files found in the tree on top of
    the built-in rules.
    """
    root = normalize_path(str(root))
    result = ScanResult(root=root)
    seen = 0

    # Project roots found so far, relative to the scan root ("" is the scan
    # root itself). os.walk is top-down, so a parent is always recorded before
    # its children need it.
    project_roots = {""}

    # One stack per directory, since a .gitignore only governs its own subtree.
    # Keyed by directory; each child inherits a copy of its parent's stack.
    stacks: dict[str, GitignoreStack] = {"": GitignoreStack()}
    gitignore_on = use_gitignore and gitignore_available

    for dirpath, dirnames, filenames in os.walk(root):
        # Path of the current directory relative to the root, for path rules.
        # "." at the root itself, which would break the prefixes below.
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        prefix = f"{rel_dir}/" if rel_dir else ""

        if is_project_root(set(dirnames) | set(filenames)):
            project_roots.add(rel_dir)

        base = _nearest_project_root(rel_dir, project_roots)

        stack = stacks.pop(rel_dir, GitignoreStack())
        if gitignore_on and GITIGNORE_NAME in filenames:
            stack = stack.copy()
            try:
                stack.add(
                    rel_dir,
                    (Path(dirpath) / GITIGNORE_NAME).read_text(
                        encoding="utf-8", errors="replace"
                    ),
                )
            except OSError:
                pass  # an unreadable .gitignore just means no extra rules

        kept = []
        for d in dirnames:
            rel_path = f"{prefix}{d}"
            if is_ignored_dir(d, rel_path, _strip_base(rel_path, base)):
                result.pruned_dirs.append(rel_path)
            elif stack.matches(rel_path, is_dir=True):
                result.pruned_dirs.append(rel_path)
            else:
                kept.append(d)
                stacks[rel_path] = stack

        # In-place assignment is required: os.walk reads this list back.
        dirnames[:] = kept

        for name in filenames:
            file_path = Path(dirpath) / name
            rel_path = file_path.relative_to(root).as_posix()

            # A file can be interesting in three different ways, and being
            # none of them is the common case worth rejecting first.
            language = detect_language(file_path.suffix)
            manifest = is_manifest(name)
            doc = is_doc_file(name, rel_path)

            if language is None and not manifest and not doc:
                continue

            if stack.matches(rel_path, is_dir=False):
                result.skipped.append(SkippedFile(rel_path, "listed in .gitignore"))
                continue

            seen += 1
            if on_progress is not None:
                on_progress(seen, rel_path)

            try:
                size = file_path.stat().st_size
            except OSError as exc:
                result.skipped.append(SkippedFile(rel_path, f"unreadable: {exc.strerror}"))
                continue

            if size > HARD_SIZE_LIMIT:
                result.skipped.append(
                    SkippedFile(rel_path, f"over hard size limit ({size // 1_000_000} MB)")
                )
                continue

            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.skipped.append(SkippedFile(rel_path, f"unreadable: {exc.strerror}"))
                continue

            if manifest:
                result.dependencies.extend(parse_manifest(name, text, rel_path))

            if doc:
                result.docs.append(
                    DocFile(rel_path=rel_path, size=size, lines=text.count("\n") + 1)
                )
                continue

            if language is None:
                continue  # a manifest that is not also source code

            reason = generated_reason(name, text)
            if reason is not None:
                result.skipped.append(SkippedFile(rel_path, reason))
                continue

            result.files.append(
                FileInfo(
                    path=file_path,
                    rel_path=rel_path,
                    language=language,
                    size=size,
                    lines=text.count("\n") + 1,
                )
            )

    result.files.sort(key=lambda f: f.rel_path)
    result.docs.sort(key=lambda d: d.rel_path)
    result.dependencies.sort(key=lambda d: (d.ecosystem, d.name.lower()))
    result.skipped.sort(key=lambda f: f.rel_path)
    result.pruned_dirs.sort()
    return result
