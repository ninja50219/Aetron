"""Applying the .gitignore files a project already ships.

A project declares its own generated output better than any built-in list can.
Git semantics are subtle (negation with "!", "**", anchoring, directory-only
patterns), so this delegates to pathspec rather than re-implementing them.

A .gitignore applies to its own directory and everything below it, so several
can be in force at once and the deepest one wins.
"""

from dataclasses import dataclass, field

try:
    import pathspec

    AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    AVAILABLE = False

GITIGNORE_NAME = ".gitignore"

def _pick_pattern_factory() -> str:
    """Choose the pattern style this pathspec version prefers.

    pathspec renamed "gitwildmatch" to "gitignore"; the old name still works
    but emits a DeprecationWarning. Probing beats a version comparison.
    """
    if not AVAILABLE:  # pragma: no cover - optional dependency
        return "gitwildmatch"

    for factory in ("gitignore", "gitwildmatch"):
        try:
            pathspec.PathSpec.from_lines(factory, ["probe"])
        except Exception:
            continue
        return factory

    return "gitwildmatch"  # pragma: no cover - pathspec changed more than expected


PATTERN_FACTORY = _pick_pattern_factory()


@dataclass
class GitignoreStack:
    """The .gitignore files in force, keyed by the directory that holds them."""

    # (directory relative to scan root, compiled spec), deepest last.
    _specs: list[tuple[str, "pathspec.PathSpec"]] = field(default_factory=list)

    def add(self, rel_dir: str, text: str) -> None:
        """Compile a .gitignore found in ``rel_dir``."""
        if not AVAILABLE:
            return
        spec = pathspec.PathSpec.from_lines(PATTERN_FACTORY, text.splitlines())
        if spec.patterns:
            self._specs.append((rel_dir, spec))

    def matches(self, rel_path: str, is_dir: bool) -> bool:
        """True if any applicable .gitignore ignores this path.

        ``rel_path`` is relative to the scan root. Git matches a path against
        the .gitignore of every ancestor directory, each time relative to that
        directory, which is what the re-basing below does.
        """
        if not self._specs:
            return False

        # Git only matches directory patterns like "build/" against a path
        # carrying the trailing slash.
        candidate = f"{rel_path}/" if is_dir else rel_path

        for base, spec in self._specs:
            if not base:
                relative = candidate
            elif candidate.startswith(f"{base}/"):
                relative = candidate[len(base) + 1 :]
            else:
                continue  # this .gitignore governs a different subtree

            if spec.match_file(relative):
                return True

        return False

    def copy(self) -> "GitignoreStack":
        """A shallow copy, so a subtree can add rules without affecting siblings."""
        return GitignoreStack(_specs=list(self._specs))
