"""Directory ignore rules, loaded from data/ignore_dirs.txt.

Three kinds of rule, because a bare name is not always enough:

    node_modules          plain name, matched anywhere in the tree
    *.egg-info            wildcard name, matched anywhere in the tree
    /Library              path anchored at a project root only
    Library/PackageCache  path fragment, matched at any depth

The anchored form matters for ecosystems that use generic directory names.
Unity's generated "Library" is safe to skip at the project root, but a
"src/Library" elsewhere may well be real source code.

"Project root" means the nearest enclosing directory holding one of
PROJECT_MARKERS, not the directory the scan started from. Pointing the scanner
at a folder full of projects has to work exactly like scanning each of them.
"""

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

IGNORE_FILE = Path(__file__).parent / "data" / "ignore_dirs.txt"

# Files and directories that mark the root of a project. Anchored ignore rules
# are resolved relative to the nearest of these.
PROJECT_MARKERS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "projectsettings",  # unity
        "package.json",
        "pyproject.toml",
        "setup.py",
        "cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "composer.json",
        "gemfile",
    }
)


def is_project_root(entry_names: set[str]) -> bool:
    """True if a directory containing these entries looks like a project root."""
    return any(name.lower() in PROJECT_MARKERS for name in entry_names)


@dataclass(frozen=True)
class IgnoreRules:
    names: frozenset[str]
    name_patterns: tuple[str, ...]
    anchored_paths: tuple[str, ...]
    path_fragments: tuple[str, ...]


def load_rules(path: Path = IGNORE_FILE) -> IgnoreRules:
    """Parse the ignore file into the four rule buckets.

    Matching is case-insensitive: these directory names are conventions, and
    Unity in particular writes "Library" while most .gitignore templates spell
    it "[Ll]ibrary".
    """
    names: set[str] = set()
    name_patterns: list[str] = []
    anchored_paths: list[str] = []
    path_fragments: list[str] = []

    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.split("#", 1)[0].strip().lower()
        if not entry:
            continue

        entry = entry.replace("\\", "/")

        if entry.startswith("/"):
            anchored_paths.append(entry.lstrip("/"))
        elif "/" in entry:
            path_fragments.append(entry.strip("/"))
        elif any(c in entry for c in "*?["):
            name_patterns.append(entry)
        else:
            names.add(entry)

    return IgnoreRules(
        names=frozenset(names),
        name_patterns=tuple(name_patterns),
        anchored_paths=tuple(anchored_paths),
        path_fragments=tuple(path_fragments),
    )


RULES = load_rules()


def is_ignored_dir(
    name: str,
    rel_path: str = "",
    project_rel_path: str | None = None,
    rules: IgnoreRules = RULES,
) -> bool:
    """True if a directory should not be walked into.

    ``rel_path`` is the path relative to the scan root and drives the fragment
    rules. ``project_rel_path`` is the path relative to the nearest project
    root and drives the anchored rules; it defaults to ``rel_path``, which is
    correct when the scan root is itself the project root.
    """
    lowered_name = name.lower()

    # Cheapest test first: a set lookup settles the vast majority of cases.
    if lowered_name in rules.names:
        return True

    if any(fnmatch(lowered_name, pattern) for pattern in rules.name_patterns):
        return True

    if not rel_path:
        return False

    lowered_path = rel_path.replace("\\", "/").lower()

    anchored = rel_path if project_rel_path is None else project_rel_path
    lowered_anchored = anchored.replace("\\", "/").lower()

    if any(fnmatch(lowered_anchored, pattern) for pattern in rules.anchored_paths):
        return True

    return any(
        fnmatch(lowered_path, fragment) or fnmatch(lowered_path, f"*/{fragment}")
        for fragment in rules.path_fragments
    )
