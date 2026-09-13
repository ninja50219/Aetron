"""Finding the files a question is about, without reading any of them.

This is the first level a model talks to. It answers "where is login?" with a
ranked list of candidate files and the evidence for each, and it does it out of
the index alone - no source code is read here, and none is returned.

The ranking exists because the model has to decide whether to spend a level-2
request on a candidate. A list in arbitrary order would make that decision
impossible; a list with a number and a reason makes it cheap. The number is a
ranking signal and nothing more. It is not a probability, and a caller that
presents it to a user as one is misreporting it.

Two things follow from the protocol that are easy to get wrong:

*Unparsed files must still be findable.* The worked example in CLAUDE.md is
``LoginController.cs`` and there is no C# parser yet. A search that only looked
at symbols would return nothing for the one case the design was written around.
File and directory names carry real intent, so they are evidence too.

*Evidence accumulates, it does not add up.* A file holding a class called
``LoginController`` and a method called ``LoginHandler`` in a file called
``LoginController.cs`` is a better answer than any one of those alone, but it
is not three times better, and no amount of weak evidence should ever reach
certainty. Scores are combined as independent evidence, so every match moves
the score toward 1.0 without ever arriving.
"""

import re
from dataclasses import dataclass, field

from aetron.analyzer.analyzer import AnalysisResult
from aetron.analyzer.symbols import SymbolKind
from aetron.scanner.scanner import ScanResult

# How many candidates a search returns. The point of this level is to be small
# enough that the model can read all of it and pick, so a long tail of 3%
# matches is worse than useless - it is expensive noise.
DEFAULT_LIMIT = 10

# Below this, a match is not worth the model's attention. A file that merely
# mentions the term somewhere scores here.
MINIMUM_SCORE = 0.05

# Weights per kind of evidence, strongest first. These are judgements, not
# measurements: a definition named exactly what was asked for is the strongest
# signal a static index can offer, and a file that only mentions the word
# somewhere is the weakest.
WEIGHTS = {
    # The whole question, written the way code writes it. "login handler" and
    # LoginHandler are the same name with the spaces taken out, and a file or
    # definition called exactly that is the best answer a search can give.
    "symbol_query": 0.95,
    "file_query": 0.92,
    "symbol_exact": 0.90,
    "file_exact": 0.75,
    "symbol_word": 0.55,
    "file_word": 0.45,
    # A name written as one lowercase run - socketserver.py, loginhandler.py -
    # has no boundary to split on, so the word rules above cannot see the term
    # inside it. Found against the standard library: "socket server" did not
    # return socketserver.py at all, which is the one file it should have
    # returned first.
    "file_partial": 0.35,
    "path_word": 0.30,
    "symbol_partial": 0.20,
    "docstring": 0.15,
    "reference": 0.08,
}

# How much each further piece of evidence counts, after the strongest.
#
# Evidence about a file is correlated, not independent: a class called "socket"
# inside socket.py is one fact observed twice, not two facts. Combining such
# matches as though they were independent walked every plausible file to 99%
# and flattened exactly the top of the ranking that has to discriminate -
# measured against the standard library, where socket.py, socketserver.py and
# asyncio/base_events.py all tied. So the strongest match counts in full and
# each one after it counts for steeply less, by rank across the whole file
# rather than within its own kind.
RANK_DECAY = 0.45

# How many pieces of evidence of one kind and term are collected at all. A
# four thousand line file matches more of everything than a small one without
# being a better answer, and listing forty of its methods as forty reasons
# helps nobody read the result. This is the long-document problem.
MAX_REPEATS = 3

# Splits identifiers into words: LoginController, login_handler, HTTPServer and
# parse2JSON all come apart the way a reader would say them.
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]*|[a-z]+|\d+")


@dataclass
class Match:
    """One piece of evidence that a file is about the term."""

    kind: str
    # What the model should read: "class LoginController", "file name matches".
    detail: str
    weight: float
    # Where in the file, when the evidence has a location. 0 when it does not.
    line: int = 0


@dataclass
class Candidate:
    """A file that might hold the answer, and why it might."""

    rel_path: str
    language: str
    score: float
    matches: list[Match] = field(default_factory=list)
    # False when no parser exists for this language yet. The model needs to
    # know before asking for a skeleton that would come back nearly empty.
    parsed: bool = True

    @property
    def percent(self) -> int:
        """The score as a whole number, rounded down and never 100.

        Rounding down rather than to nearest keeps the promise the scoring
        makes: evidence accumulates toward certainty and never arrives, so a
        display that rounds 0.996 up to "100%" would report a certainty the
        arithmetic deliberately refuses to produce.
        """
        return min(int(self.score * 100), 99)

    @property
    def reason(self) -> str:
        """The evidence in one line, strongest first.

        One definition matching two words of a two-word question is two pieces
        of evidence and scores as two, but saying its name twice tells a reader
        nothing, so the wording is deduplicated even though the score is not.
        """
        seen = dict.fromkeys(match.detail for match in self.matches)
        return ", ".join(list(seen)[:3])

    @property
    def best_line(self) -> int:
        """The most promising line to look at, or 0 if the evidence has no
        location. A starting point only - the skeleton is what decides."""
        for match in self.matches:
            if match.line:
                return match.line
        return 0


def words(identifier: str) -> list[str]:
    """The words in an identifier, lowercased."""
    return [word.lower() for word in _WORD_RE.findall(identifier)]


def _terms(query: str) -> list[str]:
    """The query as search terms. "user login" and "userLogin" are the same
    question asked two ways, so both become ["user", "login"]."""
    return list(dict.fromkeys(words(query)))


def _joined(terms: list[str]) -> str:
    """The whole query as one identifier, or "" for a single-word query.

    A single word is already covered by the exact rules; joining it would only
    duplicate evidence for the same match.
    """
    return "".join(terms) if len(terms) > 1 else ""


def _combine(matches: list[Match]) -> float:
    """Combine evidence so that more of it always helps and none of it is ever
    enough on its own.

    Each match reduces the remaining doubt by its weight, which is the standard
    way to accumulate evidence: two independent 0.5 matches reach 0.75, not
    1.0. The arithmetic is what enforces the rule that a score never reaches
    certainty, rather than a cap bolted on afterwards.

    Matches are expected sorted strongest first, because the decay is by rank:
    corroboration is worth much less than the fact it corroborates.
    """
    doubt = 1.0
    for rank, match in enumerate(matches):
        doubt *= 1.0 - match.weight * (RANK_DECAY**rank)
    return 1.0 - doubt


class _Ledger:
    """Evidence collected per file, with the per-kind decay applied as it
    arrives rather than in a second pass.

    Files are only recorded once something actually matched. An index keyed by
    every file in the project would make every file a candidate at zero.
    """

    def __init__(self) -> None:
        self.matches: dict[str, list[Match]] = {}
        self.terms: dict[str, set[str]] = {}
        self._seen: dict[tuple[str, str, str], int] = {}

    def add(self, rel_path: str, kind: str, detail: str, term: str, line: int = 0) -> None:
        occurrence = self._seen.get((rel_path, kind, term), 0)
        self._seen[(rel_path, kind, term)] = occurrence + 1

        if occurrence >= MAX_REPEATS:
            return

        self.matches.setdefault(rel_path, []).append(
            Match(kind=kind, detail=detail, weight=WEIGHTS[kind], line=line)
        )
        self.terms.setdefault(rel_path, set()).add(term)


def _describe(kind: SymbolKind, symbol_name: str, qualified: str) -> str:
    return f"{kind.value} {qualified or symbol_name}"


def _score_symbols(
    analysis: AnalysisResult, terms: list[str], evidence: _Ledger
) -> None:
    """Evidence from definitions: the strongest kind the index holds."""
    joined = _joined(terms)

    for file_symbols in analysis.files:
        for symbol in file_symbols.symbols:
            if symbol.kind == SymbolKind.MODULE:
                # A module's name is its file's name, and the path rules score
                # that already. Counting it here would report one fact twice
                # and say it twice in the reason. Its docstring, though, is the
                # best description of the file there is.
                if symbol.docstring and any(
                    term in words(symbol.docstring) for term in terms
                ):
                    matched = next(
                        term for term in terms if term in words(symbol.docstring)
                    )
                    evidence.add(
                        file_symbols.rel_path,
                        "docstring",
                        f"file docstring mentions {matched}",
                        matched,
                        1,
                    )
                continue

            name_words = words(symbol.name)
            lowered = symbol.name.lower()
            detail = _describe(symbol.kind, symbol.name, symbol.qualified_name)

            # Split once per symbol, not once per symbol per term. A docstring
            # is the longest string in the loop and splitting it repeatedly was
            # most of what a search cost.
            docstring_words = set(words(symbol.docstring)) if symbol.docstring else ()

            # Compared on words, not on the raw name: login_handler,
            # loginHandler and LoginHandler are one name spelled three ways,
            # and "login handler" is the same name spelled a fourth.
            if joined and "".join(name_words) == joined:
                evidence.add(
                    file_symbols.rel_path, "symbol_query", detail, joined, symbol.line
                )
                # The per-term rules below would match this same name again,
                # once per word. That is one fact, and counting it twice is
                # what the rank decay exists to prevent.
                continue

            for term in terms:
                if lowered == term:
                    evidence.add(
                        file_symbols.rel_path, "symbol_exact", detail, term, symbol.line
                    )
                elif term in name_words:
                    evidence.add(
                        file_symbols.rel_path, "symbol_word", detail, term, symbol.line
                    )
                elif term in lowered:
                    evidence.add(
                        file_symbols.rel_path, "symbol_partial", detail, term, symbol.line
                    )
                elif term in docstring_words:
                    evidence.add(
                        file_symbols.rel_path,
                        "docstring",
                        f"{detail} mentions {term}",
                        term,
                        symbol.line,
                    )


def _score_references(
    analysis: AnalysisResult, terms: list[str], evidence: _Ledger
) -> None:
    """Evidence from names a file merely uses.

    Deliberately the weakest signal. A file that calls ``login()`` is about
    login in a way worth mentioning, but the file that *defines* it is the
    answer, and this must never outrank that.
    """
    wanted = set(terms)

    for file_symbols in analysis.files:
        # One pass over the references builds a word index for the file, so
        # each term is a lookup rather than another pass. A large file has
        # thousands of references and this used to run once per term.
        by_word: dict[str, str] = {}
        for reference in file_symbols.references:
            for word in words(reference):
                if word not in wanted:
                    continue
                if word not in by_word or reference < by_word[word]:
                    by_word[word] = reference

        for term in terms:
            hit = by_word.get(term)
            if hit is not None:
                evidence.add(file_symbols.rel_path, "reference", f"uses {hit}", term)


def _score_paths(
    scan_result: ScanResult, terms: list[str], evidence: _Ledger
) -> None:
    """Evidence from names people chose for files and directories.

    This is the only evidence available for a language with no parser yet, and
    it is not weak evidence: a file called ``LoginController.cs`` is about
    login regardless of whether anything can read it.
    """
    joined = _joined(terms)

    for file_info in scan_result.files:
        parts = file_info.rel_path.split("/")
        filename = parts[-1]
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        stem_words = words(stem)
        directories = parts[:-1]

        if joined and "".join(stem_words) == joined:
            evidence.add(
                file_info.rel_path, "file_query", f"file is named {stem}", joined
            )
            continue  # the per-term rules below describe the same file name

        for term in terms:
            if stem.lower() == term:
                evidence.add(
                    file_info.rel_path, "file_exact", f"file is named {stem}", term
                )
            elif term in stem_words:
                evidence.add(
                    file_info.rel_path, "file_word", f"file name {filename}", term
                )
            elif term in stem.lower():
                evidence.add(
                    file_info.rel_path, "file_partial", f"file name {filename}", term
                )
            else:
                folder = next(
                    (d for d in directories if term in words(d)), None
                )
                if folder is not None:
                    evidence.add(
                        file_info.rel_path, "path_word", f"in {folder}/", term
                    )


def search(
    scan_result: ScanResult,
    analysis: AnalysisResult,
    query: str,
    limit: int = DEFAULT_LIMIT,
) -> list[Candidate]:
    """Rank the files most likely to answer a question about ``query``.

    Returns candidates only: file, score, and the evidence behind the score.
    No source code is read, and the answer is never decided here.
    """
    terms = _terms(query)
    if not terms:
        return []

    joined_query = _joined(terms)

    evidence = _Ledger()
    _score_symbols(analysis, terms, evidence)
    _score_paths(scan_result, terms, evidence)
    _score_references(analysis, terms, evidence)

    languages = {f.rel_path: f.language for f in scan_result.files}
    parsed = {f.rel_path for f in analysis.files}

    candidates = []
    for rel_path, matches in evidence.matches.items():
        matches.sort(key=lambda m: (-m.weight, m.line, m.detail))

        # A file matching every term of a multi-word question is a better
        # answer than one matching half of it, however strongly.
        matched = evidence.terms[rel_path]
        if joined_query in matched:
            matched = matched | set(terms)
        coverage = len(matched & set(terms)) / len(terms)
        score = _combine(matches) * (0.5 + 0.5 * coverage)

        if score < MINIMUM_SCORE:
            continue

        candidates.append(
            Candidate(
                rel_path=rel_path,
                language=languages.get(rel_path, "unknown"),
                score=score,
                matches=matches,
                parsed=rel_path in parsed,
            )
        )

    # Shallower files break ties: a project puts what matters near the top.
    # rel_path last, so the order never depends on dictionary insertion.
    candidates.sort(key=lambda c: (-c.score, c.rel_path.count("/"), c.rel_path))
    return candidates[:limit]
