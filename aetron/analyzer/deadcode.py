"""Finding definitions nothing appears to use.

Deliberately framed as *candidates*, never as a verdict. A static reference
scan cannot see getattr(), plugin registries, entry points declared in
packaging metadata, or anything called from another language. Reporting a
false positive as fact would make the whole tool untrustworthy, so every
result carries a confidence level and the reason it was flagged.

TODO: once ai_providers exists, offer to have a model review LOW-confidence
candidates before showing them. Recommendation: keep the static pass as the
only source of truth for HIGH confidence, and let the model only downgrade,
never upgrade - a model guessing "unused" is far more expensive to the user
than a missed dead function.
"""

from dataclasses import dataclass
from enum import Enum

from .symbols import FileSymbols, Symbol, SymbolKind


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class DeadCodeCandidate:
    rel_path: str
    symbol: Symbol
    confidence: Confidence
    reason: str


# Names that are called by the language or a framework, never by our code.
DUNDER_PREFIX = "__"

# A test runner discovers these by name, so "nothing imports it" means nothing.
TEST_PREFIXES = ("test_",)
TEST_PATH_MARKERS = ("test_", "tests/", "conftest.py")

# Entry points invoked by name from outside Python. Kept short on purpose:
# structural detection in _framework_subclasses covers the general case, and
# this list is only for module-level functions, which have no base class to
# give them away.
FRAMEWORK_NAMES = frozenset(
    {
        "main",
        "setup",
        "teardown",
        "run",
        "handler",
        "lambda_handler",
        "register",  # plugin lifecycle: Blender, pytest, many others
        "unregister",
    }
)


def find_dead_code(files: list[FileSymbols]) -> list[DeadCodeCandidate]:
    """Definitions that no file in the project refers to.

    The reference set is global on purpose. A method called only through an
    instance ("scanner.scan()") is indistinguishable from a plain call at this
    level, so matching on the bare name avoids a flood of false positives at
    the cost of missing same-named methods on different classes.
    """
    used: set[str] = set()
    dynamic_prefixes: set[str] = set()
    for file_symbols in files:
        used |= file_symbols.references
        dynamic_prefixes |= file_symbols.dynamic_prefixes

    framework_classes = _framework_subclasses(files)
    candidates = []

    for file_symbols in files:
        is_test_file = any(marker in file_symbols.rel_path for marker in TEST_PATH_MARKERS)

        for symbol in file_symbols.symbols:
            if symbol.name in used or symbol.qualified_name in used:
                continue

            owner = symbol.qualified_name.rsplit(".", 1)[0] if "." in symbol.qualified_name else ""
            if owner and owner in framework_classes:
                # An override of a base class defined outside the project is
                # called by that framework, so silence here proves nothing.
                continue

            verdict = _classify(
                symbol,
                is_test_file,
                is_framework_class=symbol.qualified_name in framework_classes,
                dynamic_prefix=_matching_prefix(symbol.name, dynamic_prefixes),
            )
            if verdict is not None:
                confidence, reason = verdict
                candidates.append(
                    DeadCodeCandidate(
                        rel_path=file_symbols.rel_path,
                        symbol=symbol,
                        confidence=confidence,
                        reason=reason,
                    )
                )

    candidates.sort(key=lambda c: (c.rel_path, c.symbol.line))
    return candidates


def _framework_subclasses(files: list[FileSymbols]) -> set[str]:
    """Qualified names of classes that extend a base defined outside the project.

    This replaces guessing from method names. A Blender operator subclassing
    bpy.types.Operator implements execute/invoke/poll because the framework
    calls them; the same is true of Django models, unittest cases and Qt
    widgets. Structure says it; a list of known method names never could.
    """
    defined_classes: set[str] = set()
    for file_symbols in files:
        for symbol in file_symbols.symbols:
            if symbol.kind == SymbolKind.CLASS:
                defined_classes.add(symbol.name)
                defined_classes.add(symbol.qualified_name)

    external: set[str] = set()

    for file_symbols in files:
        for symbol in file_symbols.symbols:
            if symbol.kind != SymbolKind.CLASS or not symbol.bases:
                continue
            for base in symbol.bases:
                # "bpy.types.Operator" and a bare "Operator" both count; only
                # the final segment can match a class defined here.
                if base.rsplit(".", 1)[-1] not in defined_classes:
                    external.add(symbol.qualified_name)
                    break

    # A class inheriting from a framework subclass is one too.
    for file_symbols in files:
        for symbol in file_symbols.symbols:
            if symbol.kind != SymbolKind.CLASS or symbol.qualified_name in external:
                continue
            if any(base.rsplit(".", 1)[-1] in external for base in symbol.bases):
                external.add(symbol.qualified_name)

    return external


def _matching_prefix(name: str, prefixes: set[str]) -> str | None:
    """The longest dynamic prefix this name starts with, if any."""
    matches = [p for p in prefixes if name.startswith(p)]
    return max(matches, key=len) if matches else None


def _classify(
    symbol: Symbol,
    is_test_file: bool,
    is_framework_class: bool = False,
    dynamic_prefix: str | None = None,
) -> tuple[Confidence, str] | None:
    """Decide how much to trust "this name never appears" for one symbol."""
    name = symbol.name

    # Python calls these itself; absence of a reference proves nothing.
    if name.startswith(DUNDER_PREFIX) and name.endswith(DUNDER_PREFIX):
        return None

    if is_test_file or name.startswith(TEST_PREFIXES):
        return None

    if dynamic_prefix is not None:
        # Checked before the private-name rule: dispatch targets are usually
        # private, and a dynamic call is much stronger evidence of use than a
        # leading underscore is of the opposite.
        return Confidence.LOW, f'may be reached dynamically via "{dynamic_prefix}"'

    if name in FRAMEWORK_NAMES:
        return Confidence.LOW, "name is a common external entry point"

    if is_framework_class:
        # Frameworks usually register their subclasses by object, not by name
        # (bpy.utils.register_class, admin.site.register). Still worth showing:
        # an operator nobody registers really is dead.
        return Confidence.LOW, "framework subclass, may be registered dynamically"

    # A leading underscore is a promise that nothing outside uses it, so the
    # project's own reference set is the whole story.
    if name.startswith("_"):
        return Confidence.HIGH, "private name unused inside the project"

    if symbol.kind == SymbolKind.VARIABLE:
        # Constants get re-exported and read from other languages often enough
        # that a bare name miss is weak evidence.
        return Confidence.LOW, "module-level name never read"

    if symbol.kind == SymbolKind.METHOD:
        # Could be an interface implementation or a framework hook.
        return Confidence.MEDIUM, "method never called by name"

    return Confidence.MEDIUM, "never referenced in the project"
