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
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from aetron.ai_providers import Message, Provider, ProviderError
from aetron.analyzer.analyzer import AnalysisResult
from aetron.context.search import search
from aetron.context.source import get_source
from aetron.context.structure import build_structure, render
from aetron.scanner.scanner import ScanResult

# How many commands a model may issue before it has to answer. Reached in
# practice only when a model is lost, and a lost model that is not stopped
# will search forever.
MAX_STEPS = 12

SEARCH_LIMIT = 8

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
class Answer:
    """The end of a conversation, and the record of how it got there."""

    question: str
    text: str = ""
    steps: list[Step] = field(default_factory=list)
    # Set when the model never answered, rather than answered badly.
    incomplete: str = ""

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
        return render(build_structure(self.scan_result, self.analysis, rel_path)), False

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

        if command == "ANSWER":
            answer.text = argument
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
