# Aetron

Aetron reads a codebase the way a new developer would like to: it works out
what the project contains, how the pieces refer to each other, and what
actually matters — so that an AI model can be asked about the project without
being handed every line of it.

## Project Status

Early development, but the whole pipeline now runs end to end: you can ask a
question in English and get back a file and a line number, or read the whole
project as one offline report. Python, C#, JavaScript, TypeScript and
Lua/Luau are parsed; the other nine languages the scanner recognises are found
by name but not yet read.

| Stage | Module | Status |
|---|---|---|
| Scan the project | `aetron/scanner` | working |
| Filter noise and generated code | `aetron/scanner` | working |
| Parse structure into symbols | `aetron/analyzer` | working (Python, C#, JS/TS, Lua) |
| Link files, imports, inheritance | `aetron/analyzer` | working |
| Build an optimised representation | `aetron/context` | working |
| Report a whole project offline | `aetron/context` | working |
| Send only relevant context to a model | `aetron/ai_providers` | working (Ollama, Claude, GPT, Gemini) |

## Problem

Opening an unfamiliar repository of fifty thousand lines, the questions that
matter are simple — where does this start, what depends on what, which of this
is still alive — and answering them by reading files takes days.

Handing the whole repository to a language model does not work either. Most of
it is dependencies, build output and generated code; the context window fills
with noise long before the interesting parts arrive.

## Solution

Aetron reduces a repository to a structured index before any model sees it.

```
scan -> filter -> parse -> index -> context -> AI
```

The reduction is real: pointed at a folder of hobby projects containing a Unity
game, 3990 source files became 5 — the rest was Unity's package cache, build
output and editor state.

## Features

- [x] Multi-language project scanning
- [x] Filtering of irrelevant files, honouring the project's own `.gitignore`
- [x] Detection of generated and minified files
- [x] Dependency manifests: npm, pip, poetry, cargo, go, csproj, maven, composer
- [x] Symbol index: classes, methods, functions, module variables
- [x] Import graph, entry points, most-depended-on files
- [x] Dead code detection with confidence levels
- [x] Ranked file search with a match percentage and the reason for it
- [x] File skeletons: every definition and its line numbers, no code
- [x] Source retrieval one definition at a time
- [x] Offline project report, stating what the analysis could not cover
- [x] C#, JavaScript, TypeScript and Lua/Luau parsers
- [x] Roblox projects: `.luau`, and Wally's vendored `Packages` left out
- [x] Support for local models (Ollama: Llama, Qwen, DeepSeek)
- [x] Support for API models (Claude, GPT, Gemini), keys read from the environment only
- [ ] Parsers for the remaining nine languages
- [ ] Documentation generation
- [ ] Potential bug detection

## Installation

Python 3.11 or newer.

```bash
pip install -r requirements.txt
```

The only dependency is `pathspec`, and it is optional: without it Aetron falls
back to its built-in ignore rules instead of reading `.gitignore`.

## Interactive browsing

For the visual frontend, double-click `Aetron.cmd` on Windows, or run
`python -m aetron ui`. A browser opens a local workspace with two ways to work.

**Ask Aetron** is the question box. Type `where is movement?`, pick a model,
and the page shows each request as it happens — the search, the one outline,
the one definition — then the answer: a confidence ring, the file and line, and
the body of the single method it points at. Clicking the location opens that
file in the explorer. With Ollama selected nothing leaves the machine; the
chip beside the model says so, and says the opposite when you choose a hosted
provider. A local model can take minutes, and the page stays usable while it
works.

**Code explorer** is the same three levels by hand: recent projects, search
results, clickable definitions, a source viewer, project overview and
omitted-file reports. Keep the terminal open while using it;
Ctrl+C stops the server. `--no-browser` prints the URL without opening it,
and `--port 8765` selects a fixed port instead of an automatically chosen one.
The server listens only on `127.0.0.1`, uses no external assets or services,
and shares project history with the terminal menu. Search spelling matters:
the existing index matches names and docstrings, not synonyms or fuzzy terms.

Run `python -m aetron` without arguments to open the menu. Choose a recent
project by number, or enter a new project directory once. Recent paths are
saved in `~/.aetron/recent-projects.json`; unavailable paths remain listed so
temporarily disconnected projects are not forgotten.

Choose **Find something**, enter a search term, choose a matching file, then
choose a definition to read its numbered source. File paths and symbol names
are always selected by number. **Explore the project** shows the offline
report and its limitations; **Review omitted files** lists skipped files with
their reasons. Pruned directories are listed when loading the project.

The menu scans and analyzes once per project selection. After editing code,
choose **Rescan the project** to refresh the index. Enter `0` to go back;
Ctrl+C or end-of-input exits from any prompt. The menu uses plain English text
without terminal colors and requires only the standard library.

Definitions sharing the same qualified name cannot be selected individually
through the current source API. The menu reports this limitation instead of
showing a different definition. All eight explicit CLI commands and the
legacy `python -m aetron PATH` scan shortcut remain available.

## Usage

List what a project contains:

```bash
python -m aetron scan /path/to/project
```

Omit the path and Aetron asks for one, which avoids fighting the shell over
paths with spaces.

Useful flags: `-q` for a summary only, `--show-skipped` and `--show-pruned` to
see exactly what was left out and why, `--show-deps` for the dependency list.

Build the symbol index and import graph:

```bash
python -m aetron analyze /path/to/project
```

Explain the project's measured structure without sending code to an AI service:

```bash
python -m aetron explain /path/to/project
```

The report lists key files, the local import edge count, declared dependencies, documentation
and structural findings. It also shows unsupported file types, parse errors
and exclusion counts. Modules with no incoming imports are reading candidates,
not confirmed runtime entry points. Mutually reachable import groups do not
by themselves prove that a program fails at runtime. Lists selected by the
summary (key files, reading candidates, dependencies and docs) retain up to ten
items; the report is not a complete inventory.

Find every definition of a name:

```bash
python -m aetron analyze /path/to/project --symbol scan
```

Report definitions nothing appears to use:

```bash
python -m aetron analyze /path/to/project --dead-code
```

Results are graded `high`, `medium` or `low`; `--confidence low` shows
everything. Nothing is presented as certain, because static analysis cannot see
`getattr`, plugin registries or calls from another language.

## Asking a question

The point of the index is that a model can answer a question about the project
without being given the project.

```bash
python -m aetron ask /path/to/project "where is login?"
```

```
  ->  SEARCH login
  ->  STRUCTURE Controllers/LoginController.cs
  ->  SOURCE Controllers/LoginController.cs LoginHandler

Login is handled in Controllers/LoginController.cs, LoginHandler at line 18.

  Controllers/LoginController.cs:18  -  method LoginController.LoginHandler, lines 18-34
  confidence 100% (4 of 4 checks passed)
  aetron source /path/to/project Controllers/LoginController.cs LoginController.LoginHandler

(3 requests; source read from: Controllers/LoginController.cs)
```

The model never receives the repository. It searches, reads one file's shape,
then asks for one definition — and the line it prints is one you can open.

The last two lines are Aetron's, not the model's. The sentence is prose and
cannot be opened, so the answer is resolved back into one definition using only
the steps the model actually ran: a model that names a file it never searched
gets no citation at all. The confidence is four checks — the file came back
from a search, its outline lists this definition at these lines, the model read
it, and the answer names it — so 100% means every check that could be made was
made and held. It is not a probability that the answer is right.

### A local model (the default)

By default this runs against a local model through
[Ollama](https://ollama.com), so nothing leaves the machine. Install Ollama
from its website, then:

```bash
ollama serve                    # the desktop app starts this for you
ollama pull qwen2.5-coder       # about 4.7 GB; qwen2.5-coder:3b if RAM is tight
python -m aetron ask /path/to/project "where are passwords hashed?"
```

`--model` picks any other model you have pulled. Aetron asks Ollama for a
16384-token context, so a long question does not silently lose its opening
message, and caps each reply at 1024 tokens. On a laptop CPU a 7B model takes
tens of seconds per step.

Whether a given local model follows Aetron's command language is something
you can measure in a minute:

```bash
AETRON_OLLAMA_MODEL=qwen2.5-coder python -m pytest tests/test_ollama_live.py -v -s
```

### A hosted model, with an API key

Four providers: `ollama` (local), `anthropic` (Claude), `openai` (GPT) and
`gemini`. A hosted model receives your question and the few lines it asks to
see, never the repository, and anything shaped like an API key in those lines
is replaced before it is sent.

| Provider | Key variable | Model |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY`, or `ant auth login` | `claude-opus-5` by default; e.g. `--model claude-opus-5-5` |
| `openai` | `OPENAI_API_KEY` | required: `--model <name>` |
| `gemini` | `GEMINI_API_KEY` | required: `--model <name>` |

```bash
pip install anthropic                          # only for --provider anthropic
export OPENAI_API_KEY="..."                    # Windows: setx OPENAI_API_KEY "..."
python -m aetron ask . "where is login?" --provider openai --model <model>
```

Keys come from **environment variables only**. Aetron never reads one from a
file, never writes one anywhere, never asks for one in the page, and scrubs it
from any error it shows. Hosted models must be named because the model decides
what you are billed. `OPENAI_BASE_URL` points the `openai` provider at any
other service that speaks the same API; a key is only ever sent over HTTPS, or
to a server on this machine.

**Keeping keys out of git.** Do not put a key in a `.env` or any other file
inside a project. If you do anyway, `.gitignore` already excludes `.env`,
`*.key`, `*.pem`, `secrets.*` and `credentials.json`, and
`tests/test_no_secrets.py` fails the test suite if anything shaped like a key
is tracked or about to be.

### The three levels, by hand

`ask` drives three commands you can also run yourself. Each is more expensive
than the last, which is why each is a separate command.

```bash
python -m aetron search /path/to/project "login"        # ranked candidates
python -m aetron structure /path/to/project auth/login.py   # definitions, no code
python -m aetron source /path/to/project auth/login.py login_handler
```

`search` returns files with a match percentage and the reason for it. A file
whose language has no parser yet is still found by name, and says so. Add
`--json` to any of the three for a machine-readable form.

Level 2 costs about an eighth of the file it describes, and a wrong guess at
level 1 costs one skeleton rather than one file. That is the whole economy of
the thing.

## What a project contains

```bash
python -m aetron summary /path/to/project
```

Size, languages, the files most depended on, notable dependencies, and
structural findings: circular imports, orphan modules, over-central files.

## Architecture

Two rules shape the code.

**Nothing is dropped silently.** Every file left out is recorded with a reason,
every pruned directory is listed. A tool that quietly hides source code cannot
be trusted with an unfamiliar project.

**No layer knows about the layer above it.** The scanner returns data and never
prints; the analyzer takes a scan result and returns an index. The CLI is one
consumer of that data, and a GUI would be another — neither requires changing
anything below.

```
aetron/
├── scanner/          walk the tree, decide what counts as source
│   ├── paths.py      normalise whatever the user typed
│   ├── languages.py  extension -> language
│   ├── ignore.py     directory rules, anchored to detected project roots
│   ├── gitignore.py  the project's own .gitignore, via pathspec
│   ├── detect.py     generated and minified file heuristics
│   ├── manifests.py  dependency extraction, eight ecosystems
│   ├── docs.py       documentation, kept apart from source
│   └── scanner.py    the walk itself; all file I/O lives here
├── analyzer/         turn source into structure
│   ├── symbols.py    the vocabulary every language parser produces
│   ├── python_parser.py      Python, via the standard ast module
│   ├── csharp_parser.py      C#, by pattern and brace counting
│   ├── javascript_parser.py  JavaScript and TypeScript, the same way
│   ├── lua_parser.py         Lua and Luau, counting "end" not braces
│   ├── references.py  a declaration is not a use of itself
│   ├── resolver.py   imports -> edges, dotted modules and paths alike
│   ├── deadcode.py   unused definitions, with confidence levels
│   └── analyzer.py   parse every file, then link them
├── context/          the retrieval protocol, and the offline report
│   ├── search.py     level 1: rank files, with the reason for each
│   ├── structure.py  level 2: one file's shape, no code
│   ├── source.py     level 3: one definition's code
│   ├── insights.py   cycles, orphans, hubs
│   ├── summary.py    what a newcomer reads first
│   └── render.py     the summary as an English report, claims nothing extra
├── ai_providers/     local models and API models, behind one method
├── credentials.py    what an API key looks like, so none is sent or pushed
├── ask.py            the model drives the levels, the rules are enforced
│                     here, and the answer is resolved to one definition
├── web.py            the local server behind `aetron ui`
├── web_ui/           one page: ask a question, or browse the levels
└── cli/              argument parsing and reporting
```

Two decisions worth knowing about:

*Anchored ignore rules resolve against the nearest project root*, not the
directory the scan started from. Unity's generated `Library` is safe to skip at
a project root while `src/Library` elsewhere may be real source, and scanning a
folder of ten projects has to behave like scanning each of them.

*Dead code detection uses inheritance, not name lists.* A class extending a base
defined outside the project implements methods that framework calls, so those
methods are never reported. This came from a real false positive: a Blender
addon produced 32 bogus results, and the structural rule removed all of them.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

Adding a language means writing a parser that takes source text and returns
`FileSymbols`, then adding one entry to `PARSERS` in `analyzer/analyzer.py`.
Nothing else changes.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: branch from current
`main` before you start, one branch per task, and run the suite both with and
without `pathspec` before you push.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
