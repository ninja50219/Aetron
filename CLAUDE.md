# CLAUDE.md

Guidance for AI agents working on Aetron. Read this first; it records the
design decisions, the current state, and where the previous session stopped.

## Where things stand

Last verified 2026-09-23 by running the suite, not by reading this file.

- `main` is the branch to start from. Five others exist:
  `claude/sleepy-galileo-8gywyf` holds the 2026-09-23 work below (the real
  Ollama run, the hosted providers, the secret guards) until it is merged;
  `claude/gallant-bell-s94tns` has tracked `main` commit for commit; and
  `feat/interactive-menu`, `feat/scanner` and `claude/funny-dijkstra-5snaae`
  sit behind it. Confirm rather than believe this - a line like this one is
  stale the moment somebody pushes:

  ```bash
  git fetch origin --prune
  for b in $(git ls-remote --heads origin | sed 's#.*refs/heads/##'); do
      git merge-base --is-ancestor origin/$b origin/main \
          && echo "$b: contained in main" || echo "$b: HAS WORK MAIN LACKS"
  done
  ```
- 671 tests pass and 4 skip in about 7 seconds; run the suite without
  `anthropic` and without `pathspec` too (see CONTRIBUTING.md) - the usual
  Windows setup has no `anthropic`.
  The 4 are `tests/test_ollama_live.py`, which runs only when
  `AETRON_OLLAMA_MODEL` names a pulled model.
- The pipeline runs end to end in two places. `aetron ask <project> "where is
  movement?"` answers from a terminal, and `aetron ui` opens a local page that
  does the same thing with the steps visible and the cited method on screen.
- **A real model has now run the protocol, and it keeps to the command
  language.** On 2026-09-23, after the system prompt was finally being sent
  (see the session log), the user ran `qwen2.5-coder` (7B) through the page on
  Windows. Every reply was one well-formed command, no prose, no fences. It
  still never answered: it looped on a refusal that was Aetron's fault, not
  the model's, and that is fixed. Its first automatic summary was right in
  substance and wrong in format - plain prose, then a bare `ANSWER` - and
  that is fixed too (see the session log). Whether it now reaches an answer
  end to end has not been re-run. The measurement, for any pulled model:

  ```bash
  AETRON_OLLAMA_MODEL=qwen2.5-coder python -m pytest tests/test_ollama_live.py -v -s
  ```

  `test_the_first_reply_is_a_command` is the measurement. Then ask the page a
  question about a real project and read the trail: a refused step now says
  why it was refused.

## What Aetron is for

Aetron answers questions about an unfamiliar codebase without sending the
codebase to a language model. The model asks; Aetron retrieves; the model sees
only the few lines that turn out to matter.

The worked example that drives the whole design:

> **User:** "where is login?"
> **Aetron, eventually:** "Login is handled in `LoginController.cs`,
> `LoginHandler` at line 68."

Between those two lines the model must never receive the repository.

## The retrieval protocol

This is the core architecture. It is **progressive disclosure**: four levels,
each an order of magnitude more expensive than the last, and the model decides
at each step whether escalating is worth it.

```
question                                       cost         who decides
   |
   v
L0  index        scan + filter + parse          free        Aetron, once
   |             never sent to the model
   v
map              names only, ranked, budgeted   800-3000    Aetron, once
   |             (context/overview.py)          tokens      per question
   v
L1  search       ranked candidate files         ~2 lines    model picks
   |             with a match % and a reason    per candidate
   v
L2  structure    skeleton of one file           ~13% of     model confirms
   |             symbols + line numbers          the file
   v
L3  source       the lines of one symbol        one symbol  model answers
   |                                            not one file
   v
answer          "LoginController.cs:68"
```

The middle two numbers are measured on this repository, not estimated: a
skeleton runs about 13% of its file as text and about 25% as JSON. Text is the
form to hand a model, because JSON repeats its keys once per symbol and a file
that is mostly signatures can have a JSON skeleton *larger* than itself.

**L0 — index.** The existing `scan` and `analyze` stages. The symbol index and
import graph are built once and held by Aetron. They are the thing that makes
everything below cheap. They are never handed to a model.

**The map.** Added on 2026-09-23, after the first real model - starting from
two lines about the project - searched "main" eleven times. Before its first
step the model gets a view of the index cut to a token budget: file names, the
names of what they define, where the program starts (a `__main__` guard, a
`Main` method, a Unity component, a Roblox server script), what it depends
on, and "+N more files in X/" for whatever did not fit. Names, never code.
Files the map names count as found, exactly as search results do. This is
Aider's repo map and Anthropic's "lightweight identifiers, loaded just in
time", applied to this protocol; the index itself still never leaves.

**L1 — search.** The model sends a term: `login`. Aetron matches it against
symbol names, qualified names, file stems and docstrings, and returns a ranked
list of candidate files, each with a confidence percentage and the reason it
matched:

```
LoginController.cs      92%   class LoginController, method LoginHandler
auth/session.py         64%   function start_session, docstring mentions login
ui/LoginView.xaml.cs    41%   file name matches
```

No source code. A few dozen lines for a repository of any size. The percentage
exists so the model can tell a direct hit from a guess — it is a ranking
signal, not a probability, and it should never be presented to the user as one.

**L2 — structure.** The model picks a candidate and asks for its shape. Aetron
returns the file's skeleton: the module docstring as a header, then every
symbol with its kind, name, line, end line, parameters and first docstring
line, plus the file's imports and the project files on either side of it in the
import graph. No bodies. Text by default, `--json` for a consumer that parses.

The model reads the skeleton and decides: is the answer here, or was the
candidate wrong? A wrong guess costs one skeleton, not one file.

**L3 — source.** Only now does code move. The model names a specific symbol and
gets the lines `Symbol.line` through `Symbol.end_line` — one function, not the
file it lives in. `end_line` is already recorded by the parser, so this is a
slice, not a second parse.

**The citation.** Not a level, and nothing the model can ask for: the thing
Aetron does once the model has stopped. An answer is prose, and prose cannot be
opened. So `ask.py` resolves the sentence into one definition - file, name,
kind, line range and the code between - using only the steps the model
actually ran. A model that names a file it never searched resolves to nothing,
which is correct: the sentence may be right, but a line number beside a guess
is the thing this project exists not to produce.

Each citation carries a **confidence**, which is four checks and not an
opinion:

```
ranked   25   a search returned this file (and at what %), or the map listed it
outline  25   the file's outline lists this definition, at these lines
read     30   the model read this definition before answering
named    20   the answer names the file or the definition it points at
```

100 means every check Aetron can make was made and held. It is deliberately a
different quantity from the L1 percentage next door, which ranks a guess from
names alone and never reaches 100 by design. Neither is a probability and
neither may be shown as one - the UI prints "not a probability" under the
number for exactly this reason.

**Who enforces the rules.** `ask.py` checks them, rather than asking the model
to follow them. A prompt is a request, and a model that ignored it would get
source code it never justified asking for - the exact failure the levels exist
to prevent. So `SOURCE` on a file whose structure was never read is refused,
and the refusal says what to do instead, which makes it a step the model can
recover from rather than a wasted turn.

**Rules the protocol depends on:**

- A level is never skipped. No L3 without an L2 that justified it.
- Each level is a separate call, so the expensive path is always explicit. This
  is also why `ask` should be its own CLI command rather than a flag on
  `analyze`.
- Aetron ranks and retrieves. It never decides what the answer is — that is the
  model's job, and the split is what keeps Aetron's output verifiable.
- Every answer ends in a file and a line number the user can open.

## Architecture rules

Two rules shape every module. They are not style preferences; breaking either
one has broken the tool before.

**Nothing is dropped silently.** Every skipped file carries a reason, every
pruned directory is listed. A tool that quietly hides source code cannot be
trusted with an unfamiliar project.

**No layer knows about the layer above it.** The scanner returns data and never
prints. The analyzer takes a scan result and returns an index. The CLI is one
consumer; a model is another; a GUI would be a third. None of them requires
changing anything below.

## Layout

```
aetron/
├── scanner/      walk the tree, decide what counts as source      DONE
├── analyzer/     source -> symbols, imports, dead code            DONE (Python, C#, JS/TS, Lua)
├── context/      the map, the retrieval protocol L1-L3, summary   DONE
├── ai_providers/ Ollama, Claude, and OpenAI-compatible (GPT,
│                 Gemini), behind one method; keys from env only   DONE
├── credentials.py what a key looks like; the ask loop hides one
│                 from the model, the suite refuses to push one    DONE
├── project_notes.py the project summary, kept in ~/.aetron        DONE
├── ask.py        the model drives L1-L3, and the answer is
│                 resolved to one definition; the only module
│                 that knows both halves of Aetron                 DONE
├── web.py        loopback server owning one project's index       DONE
├── web_ui/       index.html, app.css, app.js - ask, explore,
│                 overview, in a light and a dark theme             DONE
└── cli/          eight subcommands plus `ui`, and a menu when
                  run with no arguments                            DONE
```

## State

Verified by running the suite and the tool against itself and against the
standard library, not by reading the README.

**Working and tested** (671 tests, ~7s):

- `scanner/` — tree walk, four kinds of ignore rule anchored to detected
  project roots, `.gitignore` via `pathspec`, generated and minified detection,
  dependency manifests for eight ecosystems, docs collected separately.
- `analyzer/` — Python via `ast`; C#, JavaScript, TypeScript and Lua/Luau by
  pattern, the first three by counting braces and Lua by counting `end`. Import resolution for both dotted modules and path-style
  specifiers, entry points, dead code graded high/medium/low.
- `context/` — all three retrieval levels, plus `insights` and `summary`.
- `ask.py` — the loop, the enforcement, and the citation that ends it. Three
  effort levels (requests, map budget, reason lines), `FILES` and `SKIPPED`,
  a guard against repeated requests, a stop after three refusals in a row,
  observation masking past a budget, follow-up questions carried as text, and
  a token count on every answer.
- `ai_providers/` — Ollama, Anthropic, and an OpenAI-compatible class serving
  `openai` and `gemini`, behind one `complete` method. The Ollama request shape
  is verified against a real daemon; the hosted ones against a loopback server
  (and, for Claude, through the real SDK), never against the live APIs, since
  no key was available and none should be. Keys come from environment
  variables only, are sent only over HTTPS or to loopback, and are scrubbed
  from errors. Hosted OpenAI-compatible models have no default: the person
  paying names the model.
- `credentials.py` and `tests/test_no_secrets.py` — anything shaped like an
  API key is replaced before it reaches a model, and the suite fails if one is
  tracked or about to be. `.gitignore` excludes `.env`, `*.key`, `*.pem` and
  their kin as a second line.
- `cli/` — `scan`, `analyze`, `explain`, `summary`, `search`, `structure`,
  `source`, `ask`, the `ui` command, and a numbered menu when run bare.
- `web.py` and `web_ui/` — the local page. Asking runs on a thread and the
  page polls, so the single-threaded server stays answerable while a local
  model takes its minutes, and the steps appear as they happen. CSS and
  JavaScript are separate files served from a two-entry allowlist, which is
  what lets the Content-Security-Policy refuse inline styles outright.

**Not built — this is the work:**

1. **Parsers for the remaining eleven languages** `EXTENSION_MAP` knows — C,
   C++, Dart, Go, Java, Kotlin, PHP, Ruby, Rust, Scala, Swift. The scanner
   recognises sixteen languages and `PARSERS` in `analyzer/analyzer.py` has
   five entries (Python, C#, JavaScript, TypeScript, Lua), so the other eleven
   are found by name and never read. Adding one is a parser plus a line in that
   dict; `csharp_parser.py` is the worked example for a curly-brace language
   and `javascript_parser.py` for one with many ways to spell the same
   declaration. Recount from the code rather than trusting this line: it said
   "nine" and listed twelve, Lua among them, for a week after Lua landed.
2. **Documentation generation** and **potential bug detection**, from the
   README checklist.
3. **More providers**, if wanted. GPT and Gemini landed on 2026-09-23 as one
   OpenAI-compatible class; any other service speaking that API is an entry
   in `ENDPOINTS` in `ai_providers/openai_compatible.py`, and one that does
   not is one class with one method.

**Known limits, worth knowing before trusting output:**

- The C# and JavaScript parsers are not compilers and cannot be. It does not resolve types or
  expand generics, and a construct written in a way its patterns do not
  anticipate is missed. It is built to fail by omission: a missed method costs
  a search result, an invented one sends a reader to a line that means
  something else. Add a test to the language's test file for anything one
  misses rather than loosening a pattern until something matches.
- A pattern-based parser cannot tell a declaration from a use, so both run
  their identifier counts through `analyzer/references.py`, which spends one
  occurrence per declaration. Any future parser of this kind must do the same,
  or every definition becomes its own user and dead code detection silently
  reports nothing.
- C# `using` directives name namespaces, not files, so a C#-only project has a
  symbol index but almost no import graph. Entry points and hub files are
  correspondingly weak there.
- A Roblox `require` names an instance in a game tree, not a path on disk, so
  Lua requires are recorded as written and resolve to nothing. A Lua-only
  project therefore has no import graph at all, and `explain` says so under
  Limitations rather than letting the graph-shaped sections imply that nothing
  in the project uses anything else in it. Turning one into
  a file means reading the Rojo project file and reproducing its mapping; an
  invented edge would be worse than no edge.
- Search reads names and docstrings. It tolerates one slip of spelling in
  words of five letters or more - a letter dropped, added, or two swapped - so
  "movement" finds `PlayerMovment`, ranked below any exact spelling. It has
  no idea that "sign in" and "login" are the same question; a synonym is a
  model's job, not an index's, but a project's typo is not something a model
  can know.
- The `ranked` check passes by construction: only a file a search returned is
  eligible to be cited at all, so a citation's floor is 25 and three of the
  four checks are what actually vary. The check is kept because its detail
  line carries the search percentage, which is the part a reader wants; if it
  ever needs to discriminate, the thing to change is what it measures, not to
  quietly drop it and leave the weights summing to 75.
- A citation's confidence says the retrieval was sound, never that the answer
  is right. A model can read the wrong method carefully and score 100. The
  checks are there so a reader can see *what* was verified and disagree with
  the conclusion, which is why the UI lists them rather than only the number.
- A citation resolves to the narrowest definition that matches, because a
  class contains its methods and "line 7" is true of both. That is right for
  "where is movement" and wrong for a question whose answer really is the
  whole class; the outline is one click away for that case.
- The map is re-sent with every request of a question. Ollama and Anthropic
  cache it as a prefix, so it is paid for once in compute there; a hosted API
  without prompt caching bills it each time. An effort's map budget is the
  knob, and low effort's is 800 tokens.
- A reason line (`THINK:`) is requested above low effort and never sent back
  to the model; it exists for the reader. Whether a 7B model writes useful
  ones is unmeasured.
- `ask` has met one real model twice (qwen2.5-coder 7B, see above), and not
  yet end to end. Small models shorten paths - `PlayerMovment.cs` for
  `Assets/Scripts/PlayerMovment.cs` - so a file is resolved from what the
  model wrote when exactly one file it already found fits; two that fit are
  listed back to it. Nothing unsearched is ever resolved.
- Ollama caps `num_ctx` at the length a model was trained at. Aetron asks for
  16384; a model trained at 8192 gets 8192, and a long question on it can
  still lose its oldest messages. The question is restated in the system
  prompt, which Ollama keeps, so what is lost is early search results rather
  than what was asked.
- Reasoning models (qwen3, deepseek-r1) may spend the 1024-token reply
  ceiling on thinking and return nothing. Untested; the TODO in
  `ai_providers/ollama.py` has the recommendation.
- Credential hiding matches key formats by their provider prefixes. A key
  with no recognisable shape - a database password, a bespoke token - goes to
  the model like any other string.

## Conventions

**Working alongside another agent:** read [CONTRIBUTING.md](CONTRIBUTING.md)
first. Branch from current `main` before you start rather than before you push
- two agents worked this repository in parallel on 2026-09-13 and the one that
did not pull first spent an hour on a merge it could have avoided.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

- Python 3.11+. `pathspec` is the only runtime dependency and is optional.
- Every module has a docstring explaining *why* it exists, not what it does.
  Match that voice: prose, full sentences, decisions and their reasons.
- Comments record the finding that forced a decision, not a restatement of the
  code. Several were written after a real false positive; keep that habit.
- Tests are per-module under `tests/`, named for the module they cover.
- Adding a language means writing a parser that takes source text and returns
  `FileSymbols`, then adding one entry to `PARSERS` in `analyzer/analyzer.py`.
  Nothing else changes.
- Unresolved decisions go in a `TODO:` comment *with a recommendation*, so the
  next session inherits the thinking and not just the problem.

**Exercising the page without a model.** The suite covers the ask loop, but a
page is only really tested by driving it, and no environment here has had an
Ollama daemon. Swap the provider and run the real server:

```python
from aetron import web
from aetron.ai_providers.base import Provider

class Scripted(Provider):
    name, model = "ollama", "qwen2.5-coder"
    def __init__(self): self.replies = ["SEARCH movement", "STRUCTURE ...", "ANSWER ..."]
    def complete(self, system, messages):
        time.sleep(1)          # a local model is not instant; exercise the polling
        return self.replies.pop(0)

web.get_provider = lambda name, model=None, **k: Scripted()
web.main(["--no-browser", "--port", "8801"])
```

Then drive it with a headless browser and watch the console. Three things this
catches that the suite cannot: a Content-Security-Policy violation (the policy
allows no inline styles, so anything set through an element's `style` is dead
on arrival), a step trail that disagrees with the answer card, and layout that
breaks at phone width. Raise a *sleep* in the scripted `complete` rather than
removing it - answering instantly hides every polling bug there is.

Note that `index.html` is read once when the server starts, while `app.css` and
`app.js` are read per request. Editing the page markup needs a restart; editing
its styles or logic does not.

## Session log

Append one entry per session. State what landed and what the next session
should pick up.

### 2026-09-23 — a real Ollama, hosted models, and keys kept out

The task was to add local AI, prepare for hosted models chosen by API key, and
make sure no key or other secret could reach GitHub.

**Ollama, for real.** 0.34.3 installs from its GitHub release (1.4 GB with GPU
libraries, 97 MB without). No model could be pulled: the network policy
blocks `ollama.com`, `registry.ollama.ai` and `huggingface.co`, and fetching
weights from a third-party GitHub mirror was refused rather than routed
around. So the daemon was driven with GGUF probes written from scratch - a
74K-parameter llama with a byte vocabulary, in three variants: random
weights; one that repeats `x` forever; one that alternates `x` and `y`. They
cannot answer anything. Because every byte is one token, they measure exactly
what Ollama builds from a request, and that was enough to find four silent
failures:

1. **The system prompt was never sent.** `/api/chat` has no top-level
   `system` field and ignores unknown keys. A 1323-character `SYSTEM_PROMPT`
   added zero tokens to the prompt; as a `system` message it adds 1323.
   Every local model Aetron had ever driven had been asked to write a command
   language it was never shown. This alone may explain whatever a first real
   run would have gone on to report about small models and the protocol.
2. **A long question lost its question.** Ollama gives a model 4096 tokens
   unless asked, and past that drops the oldest messages but keeps the system
   prompt. An eight-request walk on this repository peaks near 5600 tokens.
   Now `num_ctx` is 16384, and the question is restated in the system prompt
   because Ollama caps `num_ctx` at the model's trained length.
3. **A looping reply ran for two hours.** Unbounded, the alternating probe
   produced 40960 tokens before Ollama's own cap; a single repeated token is
   caught sooner by Ollama's repeat limit, which answers 500. `num_predict`
   is now 1024, and a refused reply is echoed back shortened, since a
   thousand tokens of nothing per turn filled an 8192-token context in six.
4. **A slow model crashed the terminal.** urllib wraps a timeout while
   connecting, not one while waiting for the reply, so a model slower than
   300 seconds raised a bare `TimeoutError` past every handler.

Found on the way, in code that had been tested: **`SOURCE aetron/ask.py ask`
returned the whole 614-line file.** The parser records each file as a
`MODULE` symbol named after it, `get_source` matched it before the function,
and the model, the citation and the explorer all received the file. Four
files in this repository alone were affected. Modules are no longer
candidates at level 3.

**Hosted models.** `openai` and `gemini` are one OpenAI-compatible class over
urllib; `anthropic` stays on the SDK. Found through the SDK: with no key it
builds a client happily and raises a bare `TypeError` on the first request,
which killed the page's worker thread and left its question "running" - the
page polled forever and every rescan was refused. The provider now names the
missing variable, and the worker finishes its job whatever is raised. The
Anthropic provider also got `max_tokens` 16000 (Opus 5 thinks by default, and
thinking counts against it) and server-side refusal fallbacks
(`fallbacks: "default"`) for the Opus 5 and Fable 5 families, per the Claude
API documentation. The page's sidebar said "Code stays on this computer"
whatever was selected; it now follows the provider.

**Keys.** Environment variables only, never a file, never the page. Sent only
over HTTPS or to loopback. Scrubbed from every error. `aetron/credentials.py`
hides anything key-shaped from the model - a hard-coded key in someone's
project is exactly what level 3 might read - and `tests/test_no_secrets.py`
fails the suite if one is tracked or about to be, checked by planting one.

Verified: the suite with and without `pathspec` and `anthropic`; the tests
for each finding fail on the code before this session; `aetron ask` and the
page driven end to end against the real daemon, desktop and phone width, no
CSP violations. 505 tests became 573.

**Later the same day: the first real model.** The user installed Ollama on
Windows and asked the page "where is my movment script" with qwen2.5-coder.
The model kept to the command language perfectly - the question this project
had carried since its first session - and still never answered. The trail:

    SEARCH movement script
    OUTLINE PlayerMovment.cs        refused
    SEARCH PlayerMovment.cs
    OUTLINE PlayerMovment.cs        refused, and so on to twelve

The search had offered `Assets/Scripts/PlayerMovment.cs`; the model wrote the
file name alone; and the refusal said the file "has not come up in a search",
which was false, so the model searched again and again. Four fixes, each
with a test built from the model's trail:

- A file named loosely - bare name, any case, backslashes - resolves to the
  one found file it fits, and the step is recorded under the real path.
  Two that fit are listed back. The rule is unchanged: nothing unsearched.
- `SOURCE` split its argument at the first space, which broke on
  `Assets/My Scripts/...`; it splits at the last, since names have none.
- Search tolerates one slip of spelling. Replaying the trail found that the
  model had corrected "movment" to "movement", and the file is spelled
  `PlayerMovment` - so on a project without other matches the first search
  finds nothing at all. Costs about 10% of a search over the standard
  library, after caching.
- The page showed a refusal only as a colour. It now says what the model was
  told, which is how the loop above would have been obvious at a glance.

**Later still: the agent.** A second real run - "What starts the program?"
on a folder of Python scripts - went `SEARCH startup`, then `SEARCH main`
eleven times, each run again. The model had started from two lines about the
project. The user asked for an agent that understands the repository cheaply,
and research on how others do it (Aider's repo map; Anthropic's context
engineering guidance; JetBrains Research's observation masking; how chat
assistants show reasoning) shaped what landed:

- `context/overview.py`: the map above. Fitted incrementally (the first
  version re-rendered it per file, quadratic in the project), 10-34 ms on
  the standard library, public names before private helpers.
- `ask.py`, protocol v2: map and instructions first as a cacheable prefix,
  the question last; `FILES`, `SKIPPED`; repeats refused with a pointer;
  three refusals in a row stop the question; results shrink past a budget;
  efforts; `THINK:` reasons; follow-ups; token counts; `summarize()`.
- Ollama: `think` only for models whose `/api/show` lists "thinking" - sent
  blind it is an HTTP 400, measured - off at low effort, a level for gpt-oss;
  `message.thinking` shown; real token counts; `keep_alive` 30 minutes; a
  preload on project open. A model that only thinks now says so. Tested
  against the real daemon with probes built with `PARSER deepseek3`.
- Anthropic: `output_config.effort`, summarized adaptive thinking, and
  top-level `cache_control`, checked through the SDK over loopback.
- The page: an "About this project" summary written once and stored in
  `~/.aetron/summaries`; a conversation with each answer's thinking folded
  under an icon; a model list from Ollama and an effort slider that says what
  it spends; an explorer that opens on a file tree, important files first,
  one-child folders compacted, outlines grouped into functions, types and
  variables. The page may outline any indexed file - it is the owner's; the
  model is still rationed in `ask.py`.
- Found in the browser: the `ranked` check only passed for search results,
  so a perfect walk from the map scored 75. The map now counts.

Measured on a Projekty-shaped folder: the real trail cost 12 requests and
~6,800 tokens with no answer; the same question now costs 1 request (~580
tokens) from the map, or 3 (~1,950) reading `main` first.

**Then the first real summary.** The user opened the folder again and the
page wrote its "About this project" card with qwen2.5-coder. It never
arrived: 93 seconds, the card still empty. The trail showed the model had
understood the project from the map alone and said so in its first reply -
"This project seems to be a Unity-based application ... main script ...
`PlayerMovment.cs`" - as plain text, with no `ANSWER` in front. Refused, it
sent `ANSWER` alone, meaning "that"; refused again, it sent the prose again.
Three things, all Aetron's:

- The summary question said *begin with "This project seems to be"*, which
  is more specific than the rule saying *answer with ANSWER*, and a 7B model
  obeyed the more specific one. The question now shows the line it wants:
  `Reply as: ANSWER This project seems to be ...`. The system prompt got
  three example replies for the same reason.
- Prose that reads as an answer (six words or more, not a plan such as
  "let me check") is now taken as the answer when a summary was asked for,
  or when a model sends it twice running; the first time elsewhere it is
  refused with the exact line that would have worked. A bare `ANSWER`
  straight after such prose answers with that prose. A plan is never taken.
- The stop after three refusals only looked at refused commands, so a model
  alternating a reply with no command and a bare `ANSWER` ran to the end of
  its budget. It now runs after every refusal.

The replayed trail takes one step and saves the summary; the page drove the
same replay against the real daemon and filled the card at "≈799 tokens".
The trail labels a reply with no command `NO COMMAND` rather than `RETRY`,
which described what Aetron wanted rather than what the model sent.

**What the next session should pick up:**

1. **The same questions again with a real model**, end to end: the summary,
   "What starts the program?" and "where is my movment script". Only the
   summary has met a real model against the map, and only before the fix.
2. **A Java or Go parser**, as before.
3. Whether a 7B model's `THINK:` lines are worth their tokens; if not, make
   reasons a high-effort-only feature.

### 2026-09-14 — the redesign and the model, merged

Two pieces of work had been built on the same base in parallel: a frontend
redesign (light and dark themes, dialogs, keyboard shortcuts, a language
filter, CSS and JavaScript split out of `index.html` into `app.css` and
`app.js`, and a Content-Security-Policy with no `unsafe-inline` for styles),
and the question-answering above, written inline in the page the redesign
deleted. `main` now has both.

`web.py` merged on its own - the asset allowlist and the tightened policy do
not touch the ask job. The page did not: the ask panel, its styles and its
logic were ported by hand into the three new files, following the conventions
already there (`node`, `work`, `show`, `view`, the `--accent` token, the
existing `.source-code` line rendering, which the answer's code block now
reuses).

The one real casualty of the stricter policy was the confidence ring, which
set a CSS custom property on an element and so depended on an inline style.
It is now an SVG whose `stroke-dasharray` is set as a presentation attribute -
not a style at all, so nothing about it is subject to `style-src`. It also
looks better. Verified in the browser in both themes and at phone width, with
the console watched for policy violations; the only remaining 404 is the
favicon, which predates all of this.

Also fixed: the trail counted ANSWER as a request while the answer card did
not, so the same run reported 4 requests in one place and 3 in another.

**What the next session should pick up**, in the order I would take them:

1. **A real local model**, still. See "Where things stand" at the top of this
   file; nothing about that has changed and nothing else on this list matters
   as much. Ten minutes for anyone with Ollama installed.
2. **A Java or Go parser**, whichever the projects you care about are written
   in. `csharp_parser.py` is the worked example. Write the tests first: both
   pattern-based parsers grew their bugs in the same two places, a brace that
   opens and closes on one line and a declaration counted as a use of itself.
3. **A GPT or Gemini provider**, if wanted - one class, one method. The page
   picks up any name in `PROVIDERS` without further change.
4. **Delete the merged branches** when convenient. Four exist, three are dead
   weight, and the one real cost of leaving them is that the next agent may
   branch from a stale one, which already happened once: the redesign and the
   question mode were built on the same base in parallel and had to be merged
   by hand.

### 2026-09-14 — the frontend asks the model

The page had all three levels as clicks and no way to ask a question, which is
the thing the project is for. It has one now: a question goes to a provider,
the steps appear as they run, and the answer arrives as one definition with
its code, its line numbers and a confidence built from four checks.

What landed:

- `ask.py` resolves an Answer into a `Citation` - the narrowest definition the
  model actually reached, never one it merely named. `Answer.citation` is
  `None` when nothing supports a line number, and that is a real outcome
  rather than a failure: "this project has no multiplayer code" has nothing to
  cite and the UI drops the score rather than printing 0% beside a correct
  answer.
- `_without_paths` before matching names, because `PlayerMovement.cs` names
  the class `PlayerMovement` by accident and the class was outranking the
  method inside it that was the actual answer.
- `web.py` grew `ask` and `ask_status`. The job runs on a thread: a local 7B
  model answers in minutes and the single-threaded server would otherwise stop
  serving its own page while waiting. A rescan is refused while a question is
  in flight, since the job holds the index it started with.
- A citation authorizes its file in the explorer, so clicking the answer's
  location opens the outline without searching again.
- `aetron ask` prints the same citation, plus the `aetron source` command that
  shows the code. Both interfaces now end in the same place.

Verified in a browser against a small Unity-shaped project: "where is
movement?" returns `Player/PlayerMovement.cs:10`,
`method PlayerMovement.HandleWasdInput`, 100%, with the WASD body on screen.
Four paths driven end to end - the worked example, a citation opened in the
explorer, an answer with nothing to cite, and Ollama not running. 490 tests
became 505.

Still open, in the order I would take them:

1. **A real local model.** Unchanged from the last session and now the only
   thing between this and knowing whether it works: every test uses a scripted
   provider. The page makes this a ten-minute question for anyone with Ollama
   installed - open a project, type a question, watch the trail. Expect the
   system prompt to need work before the code does.
2. **A Java or Go parser.** `csharp_parser.py` is the worked example.
3. **A GPT or Gemini provider**, if wanted - one class, one method.

### 2026-09-14 — visual frontend

Extended the browsing task with `python -m aetron ui` and a Windows double-click
launcher, `Aetron.cmd`. The loopback-only standard-library server serves a local
HTML/CSS/JavaScript workspace and enforces search, outline, then source access.
Project history is shared with the terminal menu. Refresh invalidates previous
selections; source and project text are inserted as text, never executable HTML.
No scanner, analyzer, context, or dependency changes were needed.

Verified in the browser against the user's myminecraft project, from the
movment search to PlayerMovement.Update at lines 50–55. Tests: 490 passed with
pathspec; 485 passed and 5 skipped without it. The existing search spelling
and duplicate-qualified-name limitations remain; this frontend does not change
retrieval semantics.

### 2026-09-14 — interactive project browsing

Added a standard-library terminal menu for `python -m aetron` without arguments.
It remembers project paths, keeps scan and analysis results until refresh, and
passes numbered file and definition choices through the public retrieval API.
Explicit commands keep their existing dispatch. History failures and cancelled
input are reported without interrupting normal navigation. No scanner, analyzer,
or context files were changed.

Validation: 483 tests passed with pathspec; 478 passed and 5 skipped without it.
The source API cannot distinguish definitions sharing a qualified name by line
number. The menu explains this limitation instead of returning the first match
for a different numbered choice; a future API change would be needed to support
that case.

### 2026-07-29 — scan, parse, index

Stages 1–4 plus the deterministic half of the context stage, merged in PRs #1
and #2. Three design decisions came from real projects rather than fixtures: a
Unity project (3990 files, 5 of them the author's) forced ignore rules anchored
to project roots; a Blender addon (32 false dead-code reports) forced
structural detection of framework subclasses; CPython's `_markupbase` forced
treating concatenated string literals as dynamic-dispatch name prefixes, which
cut high-confidence findings from 225 to 147.

### 2026-09-13 — retrieval protocol recorded

Audited the tree against the README and found `context/` half built rather than
absent, `filters/` dead, and the pathspec tests hard-failing without an
optional dependency. Wrote down the four-level retrieval protocol, which had
lived only in conversation.

### 2026-09-13 — the protocol, built

The pipeline runs end to end. `aetron ask <project> "where is login?"` returns
`Controllers/LoginController.cs, LoginHandler at line 18`, and the model never
receives the repository.

Landed: all three retrieval levels (`context/search.py`, `structure.py`,
`source.py`); a C# parser and a JavaScript/TypeScript parser; `ai_providers/`
with Ollama and Claude behind one method; `ask.py`, which enforces the
protocol rather than asking the model to follow it; and seven CLI commands.
`filters/` is gone, `context/` is reachable, and the README no longer
describes a different program. 180 tests became 410.

Twelve bugs, most of them in code that was already merged and tested. The ones
worth remembering, because each came from running the thing rather than
reading it:

- Every string literal counted as a reference, docstrings included, so any
  definition whose name appeared in a sentence was silently marked used.
- Line counts were one too high everywhere, and the same expression set the
  divisor for the minified check, so a single-line minified file could pass as
  hand-written source.
- A UTF-8 byte order mark made a file unparseable. Visual Studio writes one by
  default, and C# is the language the worked example is written in.
- Pattern-based parsers counted a declaration as a use of itself, which turned
  dead code detection into a no-op for C# from the day it was added. The fix
  lives in `analyzer/references.py` so the next such parser inherits it.
- A type whose braces open and close on one line pushed a scope that could
  never be popped, so everything after it in the file was nested inside it.
  Found in JavaScript, then found again in C# by going to look.
- Search combined correlated evidence as though it were independent, which
  walked every plausible file to 99% and flattened the top of the ranking.
- The "pathspec is not installed" note went to stdout, so `--json` output was
  not JSON whenever an optional dependency was absent.

Aetron found one of these itself: `--dead-code` reported an unused property on
`SymbolOutline`, written earlier the same session.

**Next**, in the order I would take them:

1. **Run `ask` against a real local model.** Everything about the loop is
   covered by a scripted provider, and none of it has met a 7B model. Whether
   the command language survives contact is the biggest open question in the
   project, and the first person with Ollama installed can answer it in ten
   minutes. Expect the prompt to need work before the code does.
2. **A Java or Go parser**, whichever the projects you care about are written
   in. `csharp_parser.py` is the worked example; budget a day and write the
   tests first, because both parsers grew their bugs in the same places.
3. **A GPT or Gemini provider**, if wanted — one class, one method.
4. Nothing in `context/` needs revisiting. It is the part that has been run
   hardest and is now the most tested.
