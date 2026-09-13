# CLAUDE.md

Guidance for AI agents working on Aetron. Read this first; it records the
design decisions, the current state, and where the previous session stopped.

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
question                                       cost      who decides
   |
   v
L0  index        scan + filter + parse          free     Aetron, once
   |             never sent to the model
   v
L1  search       ranked candidate files         ~tokens  model picks
   |             with a match % and a reason
   v
L2  structure    JSON skeleton of one file      ~tokens  model confirms
   |             symbols + line numbers, no bodies       x10
   v
L3  source       the lines of one symbol        ~tokens  model answers
   |                                                     x100
   v
answer          "LoginController.cs:68"
```

**L0 — index.** The existing `scan` and `analyze` stages. The symbol index and
import graph are built once and held by Aetron. They are the thing that makes
everything below cheap. They are never handed to a model.

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
returns the file's skeleton as JSON: every symbol with its kind, name, line,
end line, parameters and first docstring line, plus the file's imports. No
bodies. This is close to a serialised `FileSymbols`, which already holds all of
it.

The model reads the skeleton and decides: is the answer here, or was the
candidate wrong? A wrong guess costs one skeleton, not one file.

**L3 — source.** Only now does code move. The model names a specific symbol and
gets the lines `Symbol.line` through `Symbol.end_line` — one function, not the
file it lives in. `end_line` is already recorded by the parser, so this is a
slice, not a second parse.

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
├── analyzer/     source -> symbols, imports, dead code            DONE (Python only)
├── context/      structural findings and the project summary      PARTIAL
├── ai_providers/ local and API models                             EMPTY
├── filters/      empty package, nothing references it             DEAD
└── cli/          scan and analyze subcommands                     PARTIAL
```

## State

Verified by running the suite and the tool against itself, not by reading the
README — the README's status table is stale and says `context` is not started
when half of it exists.

**Working and tested:**

- `scanner/` — tree walk, four kinds of ignore rule anchored to detected
  project roots, `.gitignore` via `pathspec`, generated and minified detection,
  dependency manifests for eight ecosystems, docs collected separately.
- `analyzer/` — Python symbol extraction via `ast`, import resolution to files,
  entry points, most-depended-on files, dead code graded high/medium/low.
- `context/insights.py` — circular imports (iterative Tarjan), orphan modules,
  hub files. Deterministic; no model involved.
- `context/summary.py` — `build_summary` reduces a scan plus an analysis to a
  `ProjectSummary`.
- `cli/` — `scan` and `analyze`.
- 180 tests, ~0.3s.

**Not built — this is the work:**

1. **L1 search.** Does not exist in any form. The index it needs is already
   there; what is missing is matching and ranking over it.
2. **L2 structure serialiser.** `FileSymbols` holds the data; nothing turns it
   into JSON for a model.
3. **L3 source extraction.** Nothing slices a file by `Symbol.line` and
   `Symbol.end_line`.
4. **`ai_providers/`.** Empty package, zero lines. Local models (Ollama, Llama,
   Qwen, DeepSeek) and API models (Claude, GPT, Gemini).
5. **The `ask` subcommand**, which drives L1→L3 on the model's behalf.
6. **Parsers for languages other than Python.** `PARSERS` in
   `analyzer/analyzer.py` has exactly one entry, while `EXTENSION_MAP` knows 15
   languages — so 14 are scanned and then honestly reported as unparsed.
   `LoginController.cs` is the protocol's own example and C# cannot be parsed
   yet; search over file and symbol names can be built before the parsers land,
   but L2 for a `.cs` file cannot.

**Known defects, small and worth fixing when nearby:**

- `aetron/filters/` is an empty package that nothing imports. Filtering lives in
  `scanner/ignore.py`, `gitignore.py` and `detect.py`. The directory only
  misleads.
- `context/` is built but unreachable: `context/__init__.py` is empty and no CLI
  command surfaces a summary, so nothing outside `tests/` imports it.
- `pathspec` is documented as optional, but without it four tests in
  `tests/test_scanner.py` fail rather than skip. The scanner's fallback works;
  the tests just do not know about it.
- README status table is wrong on the `context` row.

## Conventions

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

## Session log

Append one entry per session. State what landed and what the next session
should pick up.

### 2026-07-29 — scan, parse, index

Stages 1–4 plus the deterministic half of the context stage, merged in PRs #1
and #2. Three design decisions came from real projects rather than fixtures: a
Unity project (3990 files, 5 of them the author's) forced ignore rules anchored
to project roots; a Blender addon (32 false dead-code reports) forced
structural detection of framework subclasses; CPython's `_markupbase` forced
treating concatenated string literals as dynamic-dispatch name prefixes, which
cut high-confidence findings from 225 to 147.

### 2026-09-13 — retrieval protocol recorded

No code changed. Audited the tree against the README and found `context/` half
built rather than absent, `filters/` dead, and the pathspec tests hard-failing
without an optional dependency. Wrote down the four-level retrieval protocol
above, which had lived only in conversation.

**Next:** build L1 search — match a term against the existing symbol index and
return ranked candidates with a percentage and a reason. It is the first level
the model actually talks to, it needs no parser work and no provider, and every
level above it is easier to design once its output shape is fixed.
