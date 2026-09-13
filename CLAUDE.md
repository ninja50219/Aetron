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
question                                       cost         who decides
   |
   v
L0  index        scan + filter + parse          free        Aetron, once
   |             never sent to the model
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
├── analyzer/     source -> symbols, imports, dead code            DONE (Python, C#, JS/TS)
├── context/      the retrieval protocol, L1-L3, plus summary      DONE
├── ai_providers/ local and API models, behind one method          DONE
├── ask.py        the model drives L1-L3; the only module that
│                 knows both halves of Aetron                      DONE
└── cli/          seven subcommands, one per stage and level       DONE
```

## State

Verified by running the suite and the tool against itself and against the
standard library, not by reading the README.

**Working and tested** (321 tests, ~0.6s):

- `scanner/` — tree walk, four kinds of ignore rule anchored to detected
  project roots, `.gitignore` via `pathspec`, generated and minified detection,
  dependency manifests for eight ecosystems, docs collected separately.
- `analyzer/` — Python via `ast`; C#, JavaScript and TypeScript by pattern
  and brace counting. Import resolution for both dotted modules and path-style
  specifiers, entry points, dead code graded high/medium/low.
- `context/` — all three retrieval levels, plus `insights` and `summary`.
- `cli/` — `scan`, `analyze`, `summary`, `search`, `structure`, `source`.

**Not built — this is the work:**

1. **Parsers for the remaining ten languages** `EXTENSION_MAP` knows — Java,
   Go, Rust, Ruby, PHP, C, C++, Kotlin, Swift, Lua, Scala, Dart. `PARSERS` in
   `analyzer/analyzer.py` has four entries. Adding one is a parser plus a line
   in that dict; `csharp_parser.py` is the worked example for a curly-brace
   language and `javascript_parser.py` for one with many ways to spell the
   same declaration.
2. **Documentation generation** and **potential bug detection**, from the
   README checklist.
3. **More providers.** `ai_providers/` has Ollama and Anthropic. A provider is
   one class with one method, so GPT and Gemini are small additions.

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
- Search reads names and docstrings. It has no idea that "sign in" and "login"
  are the same question; a synonym is a model's job, not an index's.
- `ask` has never been run against a real local model in this repository -
  there is no Ollama daemon in the environment it was written in. The loop,
  the parsing and the enforcement are covered by a scripted provider; how well
  a 7B model actually follows the protocol is unmeasured, and the first person
  with Ollama installed should find out.

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
