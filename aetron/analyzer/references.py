"""Counting which names a file actually uses, for parsers without a real AST.

The Python parser can tell a definition from a use, because ``ast`` marks them
differently. A parser built on patterns cannot: scanning a file for identifiers
finds the name on the line that declares it just as readily as the name on a
line that calls it.

That difference is not cosmetic. Dead code detection asks "does anything refer
to this name?", and a definition that counts its own declaration as a reference
always answers yes - which silently turned the whole report off for every
language read by pattern.

So identifiers are counted rather than collected, and each declaration spends
one occurrence of its own name. What remains is uses.
"""

from collections import Counter

from .symbols import FileSymbols, SymbolKind


def references_excluding_declarations(
    identifiers: list[str], result: FileSymbols, keywords: frozenset[str]
) -> set[str]:
    """Every name the file uses, with each declaration's own name discounted."""
    counts = Counter(word for word in identifiers if word not in keywords)

    for symbol in result.symbols:
        if symbol.kind == SymbolKind.MODULE:
            # A module symbol is named after its file, not after anything
            # written in it, so it spends nothing.
            continue
        if counts.get(symbol.name):
            counts[symbol.name] -= 1

    return {name for name, count in counts.items() if count > 0}
