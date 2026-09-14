"""Answering a question about a codebase by letting a model drive the protocol.

This is the only module that knows both halves of Aetron. The retrieval levels
do not know a model exists; the providers do not know what a symbol is. They
meet here, and nowhere else, which is what keeps either replaceable.

The model does not get tools in the API sense. It writes one command per turn
in plain text, Aetron runs it and hands back what it returned. That choice is
what lets a seven-billion-parameter model on someone's laptop drive the same
protocol as a hosted one: writing SEARCH login is a thing every model can do,
and emitting a well-formed tool call is not.

The protocol's rules are enforced here rather than requested in the prompt. A
prompt is a request, and a model that ignores it would get source code it never
justified asking for - which is exactly the failure the levels exist to
prevent. So SOURCE on a file whose structure was never read is refused, and the
refusal is itself an observation the model can act on. Asking nicely is for
things that do not matter.

An answer is prose, and prose cannot be opened in an editor. So the loop ends
by resolving what the model said into one definition - a file, a name, a line
range and the code between - taken from the steps the model actually ran rather
than from anything it claimed. A model that names a file it never looked at
resolves to nothing, which is the correct outcome: the citation is evidence,
not decoration, and inventing one would make a guess look like a finding.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from aetron.ai_providers import Message, Provider, ProviderError
from aetron.analyzer.analyzer import AnalysisResult
from aetron.context.search import search
from aetron.context.source import get_source
from aetron.context.structure import FileStructure, build_structure, render
from aetron.scanner.scanner import ScanResult

# How many commands a model may issue before it has to answer. Reached in
# practice only when a model is lost, and a lost model that is not stopped
# will search forever.
MAX_STEPS = 12

SEARCH_LIMIT = 8

# What a citation's confidence is made of, out of 100. Each is a check that
# either happened or did not - never a model's opinion of itself, because a
# model asked how sure it is says "very" in the same tone whether it is right
# or wrong.
#
# The weights sum to 100 so a full score means every check Aetron can make was
# made and held, which is a different claim from the search percentage next
# door: that one ranks a guess from names alone and deliberately never reaches
# 100, while this one counts steps that either ran or did not. An answer read
# from an outline without opening the code stops at 70, which is the number
# doing its job rather than being modest.
CONFIDENCE_WEIGHTS = {
    "ranked": 25,
    "outline": 25,
    "read": 30,
    "named": 20,
}

SYSTEM_PROMPT = """\
You answer questions about a codebase you cannot see. You have no file access.
Instead you ask Aetron for information, one request at a time, and it replies.

Reply with exactly one command and nothing else. No explanation, no markdown,
no code fences.

  SEARCH <words>            Find files related to some words. Start here.
  STRUCTURE <file>          List one file's definitions and their line numbers.
  SOURCE <file> <name>      Show the code of one definition.
  ANSWER <text>             Give the final answer. Only when you are sure.

Rules:

- Start with SEARCH. You cannot ask for a file you have not found.
- STRUCTURE a file before SOURCE from it. The structure tells you which
  definition you want and whether the file is the right one at all.
- Ask for SOURCE only when the structure suggests the answer is in that exact
  definition. Reading code is the expensive step and usually unnecessary.
- If a search result looks wrong, search again with different words rather
  than reading files hoping to get lucky.
- Your answer must name a file and a line number, so the person asking can
  open it. For example: "Login is handled in LoginController.cs, LoginHandler
  at line 68."
- If the project does not appear to contain what was asked about, ANSWER
  saying so. A wrong answer is worse than no answer.
"""

_COMMAND_RE = re.compile(
    r"^\s*(SEARCH|STRUCTURE|SOURCE|ANSWER)\b[:\s]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class Step:
    """One exchange: what the model asked for, and what it got."""

    command: str
    argument: str
    observation: str
    # True when the command was refused rather than run.
    refused: bool = False


@dataclass
class Check:
    """One thing Aetron could verify about an answer, and whether it held."""

    name: str
    passed: bool
    detail: str


@dataclass
class Citation:
    """The one definition an answer points at.

    The point of the whole protocol is that an answer ends somewhere a person
    can open, so this is the shape of a finished answer: a file, a definition
    in it, the lines it spans, and the code between them. Everything here comes
    from the index or from a step the model ran, which is what makes it
    checkable rather than a second thing to trust.
    """

    rel_path: str
    qualified_name: str
    kind: str
    language: str
    line: int
    end_line: int
    # The first line returned, which may precede ``line`` when a decorator or
    # an attached comment belongs to the definition.
    start_line: int
    text: str = ""
    summary: str = ""
    confidence: int = 0
    checks: list[Check] = field(default_factory=list)
    # Why the code is missing or suspect, "" when it is neither.
    problem: str = ""

    @property
    def location(self) -> str:
        return f"{self.rel_path}:{self.line}"

    @property
    def checks_passed(self) -> int:
        return sum(1 for check in self.checks if check.passed)


@dataclass
class Answer:
    """The end of a conversation, and the record of how it got there."""

    question: str
    text: str = ""
    steps: list[Step] = field(default_factory=list)
    # Set when the model never answered, rather than answered badly.
    incomplete: str = ""
    # The definition the answer points at, when the steps support one. None is
    # a real outcome - an answer saying the project has no such thing has
    # nothing to cite, and neither does one that named a file out of thin air.
    citation: "Citation | None" = None

    @property
    def files_read(self) -> list[str]:
        """Files whose source actually left the project. The real cost."""
        return sorted(
            {
                step.argument.split()[0]
                for step in self.steps
                if step.command == "SOURCE" and not step.refused and step.argument
            }
        )


def parse_command(reply: str) -> tuple[str, str]:
    """The command in a model's reply, as (command, argument).

    Models wrap things. A reply may arrive fenced, prefixed with "Sure!", or
    with the command on its third line, and refusing all of that would fail on
    the small local models this is built for. So every line is examined and the
    first that begins with a command wins.
    """
    cleaned = reply.strip()
    cleaned = re.sub(r"^```[\w]*\n?|```$", "", cleaned, flags=re.MULTILINE).strip()

    for line in cleaned.split("\n"):
        match = _COMMAND_RE.match(line)
        if match:
            command = match.group(1).upper()
            argument = match.group(2).strip().strip("`").strip()
            if command == "ANSWER":
                # An answer may legitimately run to several lines, so it takes
                # the rest of the reply rather than just its own line.
                tail = cleaned.split(line, 1)[1].strip()
                return command, f"{argument}\n{tail}".strip() if tail else argument
            return command, argument

    return "", ""


class _Session:
    """One question, and what the model has been allowed to see so far."""

    def __init__(self, scan_result: ScanResult, analysis: AnalysisResult) -> None:
        self.scan_result = scan_result
        self.analysis = analysis
        # Files the model has found by search, and so may ask the shape of.
        self.found: set[str] = set()
        # Files whose shape it has read, and so may ask the source of.
        self.examined: set[str] = set()
        # The best search percentage each file has been ranked at. Kept because
        # a citation's confidence starts from the evidence that put the file in
        # front of the model in the first place.
        self.ranked: dict[str, int] = {}
        # Outlines already built, so resolving a citation re-reads nothing.
        self.outlines: dict[str, FileStructure] = {}
        # (file, definition) the model actually read the code of, in order.
        self.sourced: list[tuple[str, str]] = []

    def run(self, command: str, argument: str) -> tuple[str, bool]:
        if command == "SEARCH":
            return self._search(argument), False
        if command == "STRUCTURE":
            return self._structure(argument)
        if command == "SOURCE":
            return self._source(argument)
        return f"{command} is not a command.", True

    def _search(self, query: str) -> str:
        if not query:
            return "SEARCH needs something to look for."

        candidates = search(self.scan_result, self.analysis, query, limit=SEARCH_LIMIT)
        if not candidates:
            return f"Nothing in this project matches {query!r}."

        lines = []
        for candidate in candidates:
            self.found.add(candidate.rel_path)
            self.ranked[candidate.rel_path] = max(
                self.ranked.get(candidate.rel_path, 0), candidate.percent
            )
            note = "" if candidate.parsed else "  (no parser; names only)"
            lines.append(
                f"{candidate.percent:>3}%  {candidate.rel_path}  -  "
                f"{candidate.reason}{note}"
            )
        return "\n".join(lines)

    def _structure(self, rel_path: str) -> tuple[str, bool]:
        rel_path = rel_path.strip()
        if not rel_path:
            return "STRUCTURE needs a file.", True

        if rel_path not in self.found:
            # Enforced, not requested: a model that guessed a path would be
            # reading files it never had a reason to believe were relevant.
            return (
                f"{rel_path} has not come up in a search. SEARCH for it first.",
                True,
            )

        self.examined.add(rel_path)
        outline = build_structure(self.scan_result, self.analysis, rel_path)
        self.outlines[rel_path] = outline
        return render(outline), False

    def _source(self, argument: str) -> tuple[str, bool]:
        parts = argument.split(None, 1)
        if len(parts) < 2:
            return "SOURCE needs a file and the name of a definition in it.", True

        rel_path, name = parts[0], parts[1].strip()

        if rel_path not in self.examined:
            return (
                f"You have not read the structure of {rel_path}. "
                f"STRUCTURE {rel_path} first, so you know what to ask for.",
                True,
            )

        result = get_source(self.scan_result, self.analysis, rel_path, name)

        if not result.text:
            return f"{result.problem}", True

        self.sourced.append((rel_path, name))

        body = result.numbered()
        if result.problem:
            body = f"({result.problem})\n{body}"
        return f"{result.location}\n{body}", False


def ask(
    provider: Provider,
    scan_result: ScanResult,
    analysis: AnalysisResult,
    question: str,
    max_steps: int = MAX_STEPS,
    on_step=None,
) -> Answer:
    """Answer a question about a project, using a model and the three levels.

    ``on_step`` is called with each Step as it happens, so a caller can show
    the work. Nothing is printed here.

    The returned Answer carries a citation when the model's own steps support
    one, so a caller has somewhere to send the reader rather than a paragraph
    to paraphrase.
    """
    answer = Answer(question=question)
    session = _Session(scan_result, analysis)

    project = _describe(scan_result, analysis)
    messages = [Message(role="user", content=f"{project}\n\nQuestion: {question}")]

    for _ in range(max_steps):
        try:
            reply = provider.complete(SYSTEM_PROMPT, messages)
        except ProviderError as exc:
            answer.incomplete = str(exc)
            return answer

        command, argument = parse_command(reply)

        if not command:
            step = Step(
                command="",
                argument="",
                observation="Reply with one command: SEARCH, STRUCTURE, SOURCE or ANSWER.",
                refused=True,
            )
            answer.steps.append(step)
            if on_step:
                on_step(step)
            messages.append(Message(role="assistant", content=reply))
            messages.append(Message(role="user", content=step.observation))
            continue

        if command == "ANSWER" and not argument.strip():
            # A bare ANSWER would end the loop with nothing to show and no
            # error, which reads to the caller as a successful empty answer.
            step = Step(
                command="ANSWER",
                argument="",
                observation="ANSWER needs the answer after it.",
                refused=True,
            )
            answer.steps.append(step)
            if on_step:
                on_step(step)
            messages.append(Message(role="assistant", content="ANSWER"))
            messages.append(Message(role="user", content=step.observation))
            continue

        if command == "ANSWER":
            answer.text = argument
            answer.citation = _resolve_citation(
                session, scan_result, analysis, argument
            )
            step = Step(command=command, argument="", observation=argument)
            answer.steps.append(step)
            if on_step:
                on_step(step)
            return answer

        observation, refused = session.run(command, argument)
        step = Step(
            command=command, argument=argument, observation=observation, refused=refused
        )
        answer.steps.append(step)
        if on_step:
            on_step(step)

        messages.append(Message(role="assistant", content=f"{command} {argument}"))
        messages.append(Message(role="user", content=observation))

    answer.incomplete = (
        f"The model did not reach an answer within {max_steps} requests."
    )
    return answer


def _describe(scan_result: ScanResult, analysis: AnalysisResult) -> str:
    """A few lines telling the model what it is looking at.

    Deliberately small. Naming the languages and size stops a model guessing at
    a Java project when it is looking at a Python one; listing the files would
    be handing over the thing the whole protocol exists to avoid handing over.
    """
    languages = {}
    for file_info in scan_result.files:
        languages[file_info.language] = languages.get(file_info.language, 0) + 1

    summary = ", ".join(
        f"{name} ({count})"
        for name, count in sorted(languages.items(), key=lambda item: -item[1])[:5]
    )

    lines = [
        f"Project: {Path(scan_result.root).name}",
        f"{len(scan_result.files)} files, {scan_result.total_lines} lines: {summary}",
    ]

    if analysis.unparsed:
        lines.append(
            f"{len(analysis.unparsed)} files have no parser, so only their names "
            "are searchable."
        )

    return "\n".join(lines)


def _mentions(text: str, name: str) -> bool:
    """True when an answer refers to a name as a word rather than in passing.

    Both the qualified name and its last segment count, because a model told a
    file contains ``PlayerMovement.HandleWasd`` will as often write "the
    HandleWasd method". Word boundaries matter more than they look: without
    them "move" matches every "movement" in the sentence and the wrong
    definition wins.
    """
    for form in {name, name.rsplit(".", 1)[-1]}:
        if form and re.search(rf"(?<!\w){re.escape(form)}(?!\w)", text):
            return True
    return False


def _without_paths(text: str, paths) -> str:
    """The answer with the file names it mentions taken out.

    A file called PlayerMovement.cs contains a class called PlayerMovement, so
    a model that names the file names the class by accident - and the class
    outranks the method inside it that was the actual answer. Only whole file
    names are removed, so a sentence that goes on to talk about PlayerMovement
    the class still counts.
    """
    for path in sorted(paths, key=len, reverse=True):
        for form in (path, Path(path).name):
            text = text.replace(form, " ")
    return text


def _find_symbol(outline: FileStructure | None, name: str):
    """The definition ``name`` refers to in an outline, by qualified name first.

    A bare name is accepted as a fallback and only when it is unambiguous. Two
    classes in one file can each have a ``update``, and citing whichever came
    first would send a reader to a line that means something else.
    """
    if outline is None:
        return None

    for symbol in outline.symbols:
        if symbol.qualified_name == name:
            return symbol

    matches = [s for s in outline.symbols if s.name == name]
    return matches[0] if len(matches) == 1 else None


def _mentioned_lines(text: str) -> set[int]:
    """Line numbers an answer points at, written either way models write them."""
    return {int(n) for n in re.findall(r"(?:\bline\s*|:)(\d{1,7})\b", text, re.I)}


def _confidence(
    session: "_Session",
    rel_path: str,
    symbol,
    answer_text: str,
    stripped: str,
    was_read: bool,
) -> tuple[int, list[Check]]:
    """How much of what Aetron can check about a citation checked out.

    Deliberately not a probability, and not presented as one anywhere: nothing
    here knows whether the answer is correct, only whether the steps that
    produced it were the steps that should have produced it. A definition the
    model found by search, confirmed in an outline, read the code of and then
    named in its answer is as verified as this design can make it, and that is
    what 100 means - "every check passed", not "certainly right".

    The search percentage is reported inside the ranked check rather than
    scaled into the score. It measures how well a name matched a query, which
    is the right question at level 1 and the wrong one here: once the outline
    and the code have been read, how the file was first found stops being the
    evidence that matters.
    """
    percent = session.ranked.get(rel_path, 0)
    in_outline = _find_symbol(session.outlines.get(rel_path), symbol.qualified_name)
    named = _mentions(stripped, symbol.qualified_name) or _mentions(
        answer_text, Path(rel_path).name
    )

    checks = [
        Check(
            "ranked",
            percent > 0,
            f"Search ranked {rel_path} at {percent}%" if percent
            else f"{rel_path} was never returned by a search",
        ),
        Check(
            "outline",
            in_outline is not None,
            f"The file's outline lists {symbol.qualified_name} "
            f"at lines {symbol.line}-{symbol.end_line}"
            if in_outline is not None
            else "The file's outline does not list this definition",
        ),
        Check(
            "read",
            was_read,
            "The model read this definition before answering"
            if was_read
            else "The model answered from the outline without reading the code",
        ),
        Check(
            "named",
            named,
            "The answer names this file or definition"
            if named
            else "The answer does not name what it points at",
        ),
    ]

    score = sum(
        CONFIDENCE_WEIGHTS[check.name] for check in checks if check.passed
    )
    return score, checks


def _resolve_citation(
    session: "_Session",
    scan_result: ScanResult,
    analysis: AnalysisResult,
    answer_text: str,
) -> Citation | None:
    """The one definition an answer points at, or None when it points nowhere.

    Only definitions the model actually reached are eligible. That is the whole
    guard: a model that ends with "movement is in PlayerController.cs" having
    never searched for it gets no citation, and the caller shows the sentence
    without the authority of a line number attached to it.

    Fetching the code here is not the model reading it - the loop is over and
    nothing goes back into the conversation. It is the person who asked the
    question finally being shown the answer, which is what all of this was for.
    """
    lines_named = _mentioned_lines(answer_text)
    stripped = _without_paths(answer_text, session.found | session.examined)
    pool: list[tuple[int, str, object, bool]] = []

    for order, (rel_path, name) in enumerate(session.sourced):
        symbol = _find_symbol(session.outlines.get(rel_path), name)
        if symbol is not None:
            pool.append((order, rel_path, symbol, True))

    for order, rel_path in enumerate(sorted(session.examined)):
        outline = session.outlines.get(rel_path)
        if outline is None:
            continue
        for symbol in outline.symbols:
            named = _mentions(stripped, symbol.qualified_name)
            on_line = any(symbol.line <= n <= symbol.end_line for n in lines_named)
            if named or on_line:
                pool.append((order, rel_path, symbol, False))

    if not pool:
        return None

    def rank(entry) -> tuple:
        order, rel_path, symbol, was_read = entry
        return (
            was_read,
            _mentions(stripped, symbol.qualified_name),
            any(symbol.line <= n <= symbol.end_line for n in lines_named),
            # The narrowest definition that still matches. A class contains its
            # methods, so "line 7" and even the class's own name are true of
            # both - and the answer to "where is movement" is the method that
            # reads the keys, not the four hundred lines it sits in.
            -(symbol.end_line - symbol.line),
            _mentions(answer_text, Path(rel_path).name),
            session.ranked.get(rel_path, 0),
            order,
        )

    _, rel_path, symbol, was_read = max(pool, key=rank)

    confidence, checks = _confidence(
        session, rel_path, symbol, answer_text, stripped, was_read
    )
    outline = session.outlines.get(rel_path)
    slice_ = get_source(scan_result, analysis, rel_path, symbol.qualified_name)

    return Citation(
        rel_path=rel_path,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        language=outline.language if outline else "",
        line=symbol.line,
        end_line=symbol.end_line,
        start_line=slice_.start_line or symbol.line,
        text=slice_.text,
        summary=symbol.summary,
        confidence=confidence,
        checks=checks,
        problem=slice_.problem,
    )
