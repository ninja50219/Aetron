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
from aetron.context.overview import build_overview, estimate_tokens, list_folder, list_skipped
from aetron.context.structure import FileStructure, build_structure, render
from aetron.credentials import hide_credentials
from aetron.scanner.scanner import ScanResult

# How many commands a model may issue before it has to answer, when a caller
# asks for a number rather than an effort. Reached in practice only when a
# model is lost, and a lost model that is not stopped will search forever.
MAX_STEPS = 12


@dataclass(frozen=True)
class Effort:
    """How much a question may cost, chosen by the person asking.

    Effort is spent in tokens, so every knob here is one: how many requests
    the model may make, how large a map it starts from, whether it writes a
    reason before each command (readable, and a few tokens a turn), and how
    many recent results stay in full once the conversation has to shrink.
    """

    name: str
    label: str
    max_steps: int
    map_budget: int
    reasons: bool
    keep_full: int
    # Past this many characters of conversation, older results are replaced
    # by a line saying what they were. Well above what a short question
    # reaches, so an ordinary answer keeps its whole history cacheable.
    conversation_budget: int


EFFORTS = {
    "low": Effort("low", "Fast", 6, 800, False, 2, 12_000),
    "medium": Effort("medium", "Balanced", 10, 1500, True, 3, 24_000),
    "high": Effort("high", "Thorough", 16, 3000, True, 4, 48_000),
}
DEFAULT_EFFORT = "medium"

# How many refusals in a row mean a model is lost rather than learning. Measured
# on the first real model's trail replayed against the map: with every repeat
# refused, a model that ignored the refusals still spent its whole budget - ten
# requests, seven thousand tokens - asking the same thing. Three in a row is
# past the point where one more refusal would teach it anything.
STUCK_AFTER = 3

# How a finished question is carried into the next one: the question and the
# start of its answer, never the steps. A follow-up needs to know what was
# said, not what was read to say it.
HISTORY_TURNS = 3
HISTORY_ANSWER_CHARS = 400

# "Begin with 'This project seems to be'" was the first wording, and the first
# real model to write a summary did exactly that - as plain text, with no
# ANSWER in front, because the question's instruction was more specific than
# the rules'. The format now lives in the question too.
SUMMARY_QUESTION = (
    "In two or three sentences, what is this project? Say what it is for, what "
    "it is built with, and where it starts. Read a file only if the map leaves "
    "you unsure. Reply as: ANSWER This project seems to be ..."
)

SEARCH_LIMIT = 8

# How much of a reply that was not a command goes back into the conversation.
# The model is shown what it said so it can see why it was refused, but a
# looping local model sends a thousand tokens of nothing per turn, and echoing
# all of it crowded an 8192-token context within six turns on a real Ollama.
ECHO_LIMIT = 300

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
You answer questions about a codebase you cannot see. Aetron has indexed it
and shows you a map below: file names and the names of what they define. You
see code only when you ask for it, one request at a time.

Each turn, reply with one command:

  STRUCTURE <file>        One file's definitions and their line numbers.
  SOURCE <file> <name>    The code of one definition. The expensive step.
  SEARCH <words>          Find files by words, when the map is not enough.
  FILES <folder>          List a folder the map left out.
  SKIPPED                 List files the index left out, and why.
  ANSWER <text>           Your final answer.

How to work:

- Start from the map. If it already answers the question, ANSWER at once.
- Read as little as you can. When you are about 75% sure, ANSWER. Ask for
  SOURCE only when an outline does not settle it.
- Use file paths exactly as the map or a result writes them.
- Never repeat a request: its result is already above.
- Name a file and a line when there is one, so the person asking can open
  it: "Login is handled in LoginController.cs, LoginHandler at line 68."
- If the project does not contain what was asked about, say so. A wrong
  answer is worse than no answer.

Example replies, one per turn:

  STRUCTURE src/player/Movement.cs
  SOURCE src/player/Movement.cs Movement.Update
  ANSWER Movement is handled in src/player/Movement.cs, Movement.Update at line 12.
"""

# Appended by effort. A reason costs a few tokens a turn and is what lets the
# person asking read the model's thinking when the model has no thinking of
# its own to show; at the lowest effort it is not worth the tokens.
REASON_RULE = (
    "- Before the command, write one line starting with THINK: saying why, in\n"
    "  under fifteen words.\n"
)
BARE_RULE = "- Reply with the command only. No explanation, no markdown, no code fences.\n"

_COMMAND_RE = re.compile(
    r"^\s*(SEARCH|STRUCTURE|OUTLINE|SOURCE|FILES|SKIPPED|ANSWER)\b[:\s]*(.*)$",
    re.IGNORECASE | re.DOTALL,
)

# A command the page calls by a friendlier name, and a model may copy back.
_ALIASES = {"OUTLINE": "STRUCTURE"}

_COMMANDS = "SEARCH, STRUCTURE, SOURCE, FILES, SKIPPED or ANSWER"

# How a reply that is a plan rather than an answer begins. Prose is taken as an
# answer when a model insists on it; a plan never is.
_PLAN_RE = re.compile(
    r"^(let me|let's|i will|i'll|i need|i should|i must|i am going|i'm going|"
    r"first|next|to answer|we need|we should|now i)\b",
    re.IGNORECASE,
)


def _prose(reply: str) -> str:
    """A reply's text as an answer: fences and a THINK: label removed."""
    text = re.sub(r"^```[\w]*\n?|```$", "", reply.strip(), flags=re.MULTILINE).strip()
    text = re.sub(r"^(THINK|THOUGHT|REASON)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


def _answer_shaped(text: str) -> bool:
    """Whether prose could be a final answer rather than a plan or a fragment."""
    return len(text.split()) >= 6 and not _PLAN_RE.match(text)


@dataclass
class Step:
    """One exchange: what the model asked for, and what it got."""

    command: str
    argument: str
    observation: str
    # True when the command was refused rather than run.
    refused: bool = False
    # What the model said it was thinking: its own reasoning when the model
    # has some to show, otherwise the one-line reason it was asked for.
    thought: str = ""


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
    # What the question cost. Estimated from characters when the provider
    # does not report its own counts; either way, the number the whole
    # design exists to keep small, so it is measured rather than assumed.
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_estimated: bool = True
    effort: str = DEFAULT_EFFORT

    @property
    def files_read(self) -> list[str]:
        """Files whose source actually left the project. The real cost."""
        return sorted(
            {
                step.argument.rsplit(None, 1)[0]
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
            command = _ALIASES.get(command, command)
            argument = match.group(2).strip().strip("`").strip()
            if command == "ANSWER":
                # An answer may legitimately run to several lines, so it takes
                # the rest of the reply rather than just its own line.
                tail = cleaned.split(line, 1)[1].strip()
                return command, f"{argument}\n{tail}".strip() if tail else argument
            return command, argument

    return "", ""


def split_thought(reply: str) -> str:
    """The reason a model wrote before its command, without the command.

    Asked for as one line starting with THINK:, and taken from any prose
    before the command when a model wrote its reasoning without the label.
    """
    cleaned = re.sub(r"^```[\w]*\n?|```$", "", reply.strip(), flags=re.MULTILINE)
    before = []
    for line in cleaned.split("\n"):
        if _COMMAND_RE.match(line):
            break
        before.append(line.strip())
    thought = " ".join(part for part in before if part)
    thought = re.sub(r"^(THINK|THOUGHT|REASON)\s*:\s*", "", thought, flags=re.IGNORECASE)
    return thought[:300]


class _Session:
    """One question, and what the model has been allowed to see so far."""

    def __init__(
        self,
        scan_result: ScanResult,
        analysis: AnalysisResult,
        shown: set[str] | None = None,
        map_budget: int = 1500,
    ) -> None:
        self.scan_result = scan_result
        self.analysis = analysis
        self.map_budget = map_budget
        # Files the model has been shown - by the map, a search or a folder
        # listing - and so may ask the shape of.
        self.found: set[str] = set(shown or ())
        # The files the map named. A file the map led the model to has been
        # put forward exactly as a search result has, and counts as such.
        self.mapped: set[str] = set(shown or ())
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

    def run(self, command: str, argument: str) -> tuple[str, bool, str]:
        """The command's result, whether it was refused, and its argument as
        Aetron understood it.

        The argument comes back because a file named loosely is recorded under
        the path that was actually read, so the trail, the cost and the
        citation all name the real file.
        """
        if command == "SEARCH":
            return self._search(argument), False, argument
        if command == "STRUCTURE":
            return self._structure(argument)
        if command == "SOURCE":
            return self._source(argument)
        if command == "FILES":
            return self._files(argument), False, argument
        if command == "SKIPPED":
            return list_skipped(self.scan_result, self.analysis), False, ""
        return f"{command} is not a command.", True, argument

    def _files(self, folder: str) -> str:
        listing = list_folder(self.scan_result, self.analysis, folder, self.map_budget)
        self.found |= listing.shown
        return listing.text

    def _resolve(self, written: str, allowed: set[str]) -> tuple[str, str]:
        """The file a model meant among the files it may ask about.

        Returns (path, "") when ``written`` names exactly one of them, and
        ("", why) when it names several. ("", "") means it names none.

        Found on the first real model to run the protocol. The search result
        said ``Assets/Scripts/PlayerMovment.cs``, the model wrote
        ``STRUCTURE PlayerMovment.cs``, and the refusal told it the file had
        not come up in a search - which was false, so it searched again, and
        did the same thing until it ran out of requests. Small models shorten
        paths; that is not a reason to refuse what the search just offered.
        Only files already allowed are considered, so the rule the refusal
        enforced - nothing is read that no search put forward - still holds.
        """
        name = written.strip().strip("`'\"").replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        if name in allowed:
            return name, ""

        lowered = name.lower()
        matches = sorted(
            path for path in allowed
            if path.lower() == lowered or path.lower().endswith("/" + lowered)
        )
        if len(matches) == 1:
            return matches[0], ""
        if matches:
            listed = ", ".join(matches[:5])
            return "", f"{written} could be any of: {listed}. Use the full path."
        return "", ""

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

    def _structure(self, written: str) -> tuple[str, bool, str]:
        written = written.strip()
        if not written:
            return "STRUCTURE needs a file.", True, written

        rel_path, ambiguous = self._resolve(written, self.found)
        if ambiguous:
            return ambiguous, True, written
        if not rel_path:
            # Enforced, not requested: a model that guessed a path would be
            # reading files it never had a reason to believe were relevant.
            return (
                f"{written} has not come up in the map or a search. SEARCH for a "
                "word in its name, then use the path exactly as the results give it.",
                True,
                written,
            )

        self.examined.add(rel_path)
        outline = build_structure(self.scan_result, self.analysis, rel_path)
        self.outlines[rel_path] = outline
        return render(outline), False, rel_path

    def _source(self, argument: str) -> tuple[str, bool, str]:
        # Split at the last space, not the first. A definition's name never
        # contains one and a path may - "Assets/My Scripts/Player.cs" is an
        # ordinary Unity path on Windows.
        parts = argument.rsplit(None, 1)
        if len(parts) < 2:
            return "SOURCE needs a file and the name of a definition in it.", True, argument

        written, name = parts[0], parts[1].strip("`'\"")
        # "Update()" is how a model writes a method it means by name.
        if name.endswith("()"):
            name = name[:-2]

        rel_path, ambiguous = self._resolve(written, self.examined)
        if ambiguous:
            return ambiguous, True, argument
        if not rel_path:
            target = self._resolve(written, self.found)[0] or written
            return (
                f"You have not read the structure of {target}. "
                f"STRUCTURE {target} first, so you know what to ask for.",
                True,
                argument,
            )

        result = get_source(self.scan_result, self.analysis, rel_path, name)

        if not result.text:
            return f"{result.problem}", True, f"{rel_path} {name}"

        self.sourced.append((rel_path, name))

        body = result.numbered()
        if result.problem:
            body = f"({result.problem})\n{body}"
        return f"{result.location}\n{body}", False, f"{rel_path} {name}"


def ask(
    provider: Provider,
    scan_result: ScanResult,
    analysis: AnalysisResult,
    question: str,
    max_steps: int | None = None,
    on_step=None,
    effort: str = DEFAULT_EFFORT,
    history: list[tuple[str, str]] | None = None,
    notes: str = "",
) -> Answer:
    """Answer a question about a project, using a model and the three levels.

    ``on_step`` is called with each Step as it happens, so a caller can show
    the work. Nothing is printed here.

    ``effort`` names an entry in EFFORTS; ``max_steps``, when given, overrides
    its request budget. ``history`` is earlier (question, answer) pairs from
    the same conversation, and ``notes`` an earlier summary of the project -
    both are carried as text, a few hundred characters, never as the steps
    that produced them.

    The returned Answer carries a citation when the model's own steps support
    one, so a caller has somewhere to send the reader rather than a paragraph
    to paraphrase.
    """
    level = EFFORTS.get(effort, EFFORTS[DEFAULT_EFFORT])
    steps_allowed = max_steps if max_steps is not None else level.max_steps
    answer = Answer(question=question, effort=level.name)

    overview = build_overview(scan_result, analysis, budget=level.map_budget, notes=notes)
    session = _Session(scan_result, analysis, overview.shown, level.map_budget)

    # The instructions and the map come first and never change during a
    # question, or between questions about the same project: that is the
    # prefix a provider's cache can reuse, which on Ollama means the map is
    # read once rather than once per request. What changes goes last.
    #
    # The question is restated at the very end. Ollama, given more
    # conversation than its context holds, silently removes the oldest
    # messages and keeps the system prompt - and the oldest message holds the
    # question, so the model went on searching for nothing it could still see.
    rules = REASON_RULE if level.reasons else BARE_RULE
    system = (
        f"{SYSTEM_PROMPT}{rules}\nThe project:\n{overview.text}\n\n"
        f"The question you are answering: {question}\n"
    )
    messages = [Message(role="user", content=_opening(question, history))]
    # Where each result sits in ``messages``, so old ones can be shortened.
    observed: list[int] = []
    shortened: set[int] = set()
    # Each request already run, and the step that ran it.
    asked: dict[tuple[str, str], int] = {}
    reported = {"in": 0, "out": 0, "real": False}

    last_prose = ""

    def record(step: Step) -> None:
        answer.steps.append(step)
        if on_step:
            on_step(step)

    for used in range(steps_allowed):
        _shrink(messages, observed, shortened, level)
        try:
            reply = provider.complete(system, messages)
        except ProviderError as exc:
            answer.incomplete = str(exc)
            _account(answer, reported)
            return answer
        _count(provider, system, messages, reply, reported)

        thought = (getattr(provider, "last_thinking", "") or "").strip() or split_thought(reply)
        command, argument = parse_command(reply)
        remaining = steps_allowed - used - 1

        if not command:
            text = _prose(reply)
            # Found on the first real model to write a summary: it replied
            # with the summary itself, plain, then with a bare ANSWER meaning
            # "that". Prose is taken as the answer when prose was asked for,
            # or when the model sends it a second time running; once, it is
            # refused with the exact line that would have worked.
            insisted = bool(answer.steps) and answer.steps[-1].command == "" and answer.steps[-1].refused
            if _answer_shaped(text) and (question == SUMMARY_QUESTION or insisted):
                command, argument = "ANSWER", text
                last_prose = ""
                # The reply was the answer, not a reason for one.
                thought = (getattr(provider, "last_thinking", "") or "").strip()
            else:
                last_prose = text if _answer_shaped(text) else ""
                opening = " ".join(text.split()[:8])
                hint = f" If that was your answer, send it as: ANSWER {opening} ..." if last_prose else ""
                step = Step(
                    command="",
                    argument="",
                    observation=f"Reply with one command: {_COMMANDS}.{hint}",
                    refused=True,
                    thought=thought,
                )
                record(step)
                messages.append(Message(role="assistant", content=_shortened(reply)))
                messages.append(Message(role="user", content=_with_budget(step.observation, remaining)))
                if _stuck(answer, reported):
                    return answer
                continue

        if command == "ANSWER" and not argument.strip() and last_prose:
            # "ANSWER" alone, straight after an answer written without it: the
            # model is pointing at what it just said.
            argument = last_prose

        if command == "ANSWER" and not argument.strip():
            # A bare ANSWER would end the loop with nothing to show and no
            # error, which reads to the caller as a successful empty answer.
            step = Step(
                command="ANSWER",
                argument="",
                observation="ANSWER needs the answer after it, on the same line.",
                refused=True,
                thought=thought,
            )
            record(step)
            messages.append(Message(role="assistant", content="ANSWER"))
            messages.append(Message(role="user", content=step.observation))
            if _stuck(answer, reported):
                return answer
            continue

        if command == "ANSWER":
            answer.text = argument
            answer.citation = _resolve_citation(
                session, scan_result, analysis, argument
            )
            record(Step(command=command, argument="", observation=argument, thought=thought))
            _account(answer, reported)
            return answer

        key = (command, " ".join(argument.lower().split()))
        earlier = asked.get(key)
        if earlier is not None and observed and earlier_result_visible(earlier, answer, observed, shortened):
            # Found on the first real model to run the protocol: SEARCH main,
            # eleven times, each run again and each answered the same way. A
            # repeat is refused with a pointer to the result it already has.
            observation = (
                f"You already asked for this (request {earlier + 1}); its result is "
                "above. Use it: STRUCTURE a file it named, or ANSWER."
            )
            refused = True
        else:
            observation, refused, argument = session.run(command, argument)
            # The one door between the project and the model, so the one place
            # a key in someone's source is stopped. The page and the citation
            # still show the real code: they stay on this machine, and a hosted
            # model does not.
            observation, _ = hide_credentials(observation)
            if not refused:
                asked[key] = len(answer.steps)
                asked[(command, " ".join(argument.lower().split()))] = len(answer.steps)

        record(
            Step(
                command=command,
                argument=argument,
                observation=observation,
                refused=refused,
                thought=thought,
            )
        )
        messages.append(Message(role="assistant", content=f"{command} {argument}".strip()))
        messages.append(Message(role="user", content=_with_budget(observation, remaining)))
        observed.append(len(messages) - 1)

        if _stuck(answer, reported):
            return answer

    answer.incomplete = (
        f"The model did not reach an answer within {steps_allowed} requests."
    )
    _account(answer, reported)
    return answer


def _stuck(answer: Answer, reported: dict) -> bool:
    """End the question when the last few steps were all refused.

    Checked after every refusal, not only a refused command: the first real
    summary alternated between a reply with no command and a bare ANSWER,
    which the first version of this check never looked at, and ran to the
    end of its budget.
    """
    recent = answer.steps[-STUCK_AFTER:]
    if len(recent) == STUCK_AFTER and all(step.refused for step in recent):
        answer.incomplete = (
            f"The model was refused {STUCK_AFTER} times in a row and was stopped "
            "early rather than spend more tokens. A larger model, or a higher "
            "effort, usually gets further."
        )
        _account(answer, reported)
        return True
    return False


def summarize(
    provider: Provider,
    scan_result: ScanResult,
    analysis: AnalysisResult,
    effort: str = "low",
    on_step=None,
) -> Answer:
    """A short account of what the project is, written from its map.

    The same loop as any question, so the model may read a file when the map
    leaves it unsure - and at low effort by default, because a summary is
    asked for once per project and should cost as little as that deserves.
    """
    return ask(provider, scan_result, analysis, SUMMARY_QUESTION, effort=effort, on_step=on_step)


def earlier_result_visible(
    step_index: int, answer: Answer, observed: list[int], shortened: set[int]
) -> bool:
    """Whether the result of an earlier step is still in the conversation in
    full. A shortened one may be asked for again - that is what the note that
    replaced it tells the model to do."""
    result_steps = [
        i for i, step in enumerate(answer.steps)
        if step.command not in ("", "ANSWER")
    ]
    if step_index not in result_steps:
        return True
    position = result_steps.index(step_index)
    if position >= len(observed):
        return True
    return observed[position] not in shortened


def _opening(question: str, history: list[tuple[str, str]] | None) -> str:
    """The first message: earlier questions in brief, then this one."""
    lines = []
    for earlier_question, earlier_answer in (history or [])[-HISTORY_TURNS:]:
        said = " ".join(earlier_answer.split())
        if len(said) > HISTORY_ANSWER_CHARS:
            said = said[:HISTORY_ANSWER_CHARS] + " ..."
        lines.append(f"Earlier question: {earlier_question}\nYour answer: {said}")
    lines.append(f"Question: {question}")
    return "\n\n".join(lines)


def _with_budget(observation: str, remaining: int) -> str:
    """A result, with how many requests are left when that has become urgent."""
    if remaining == 1:
        return f"{observation}\n[One request left: ANSWER now with what you have.]"
    if remaining <= 3:
        return f"{observation}\n[{remaining} requests left.]"
    return observation


def _shrink(
    messages: list[Message], observed: list[int], shortened: set[int], level: Effort
) -> None:
    """Replace old results with one line each, once the conversation is long.

    The whole conversation is sent again on every request, so a result read
    at request two is paid for at requests three to twelve - which is where
    most of an agent's tokens go. Measured by JetBrains Research on SWE-bench
    agents, replacing old tool output with a placeholder while keeping the
    latest in full halved the cost and matched LLM-written summaries on solve
    rate. It is done only past a budget, so an ordinary short question keeps
    an unchanged history that a provider's cache can reuse.
    """
    size = sum(len(m.content) for m in messages)
    if size <= level.conversation_budget:
        return
    for index in observed[: -level.keep_full or None]:
        if index in shortened:
            continue
        content = messages[index].content
        first = content.split("\n", 1)[0][:120]
        lines = content.count("\n") + 1
        messages[index] = Message(
            role="user",
            content=f"[Earlier result, shortened: {first} ... ({lines} lines). Ask again if you need it.]",
        )
        shortened.add(index)
        size = sum(len(m.content) for m in messages)
        if size <= level.conversation_budget:
            return


def _count(provider, system: str, messages: list[Message], reply: str, reported: dict) -> None:
    """Add one request's cost, from the provider's own count when it has one."""
    usage = getattr(provider, "last_usage", None)
    if usage and usage[0] is not None:
        reported["in"] += int(usage[0])
        reported["out"] += int(usage[1] or 0)
        reported["real"] = True
        return
    reported["in"] += estimate_tokens(system + "".join(m.content for m in messages))
    reported["out"] += estimate_tokens(reply)


def _account(answer: Answer, reported: dict) -> None:
    answer.tokens_in = reported["in"]
    answer.tokens_out = reported["out"]
    answer.tokens_estimated = not reported["real"]


def _shortened(reply: str) -> str:
    """A refused reply as the conversation keeps it: enough to see the mistake."""
    reply = reply.strip()
    if len(reply) <= ECHO_LIMIT:
        return reply
    return f"{reply[:ECHO_LIMIT]} [... {len(reply) - ECHO_LIMIT} more characters]"


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
            percent > 0 or rel_path in session.mapped,
            f"Search ranked {rel_path} at {percent}%" if percent
            else f"The project map listed {rel_path}" if rel_path in session.mapped
            else f"{rel_path} was never returned by a search or the map",
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
