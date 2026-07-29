# Aetron

Aetron reads a codebase the way a new developer would like to: it works out
what the project contains, how the pieces refer to each other, and what
actually matters — so that an AI model can be asked about the project without
being handed every line of it.

## Project Status

Early development. The scanning and analysis stages work and are tested; the
context builder and AI providers are not written yet.

| Stage | Module | Status |
|---|---|---|
| Scan the project | `aetron/scanner` | working |
| Filter noise and generated code | `aetron/scanner` | working |
| Parse structure into symbols | `aetron/analyzer` | working (Python) |
| Link files, imports, inheritance | `aetron/analyzer` | working (Python) |
| Build an optimised representation | `aetron/context` | not started |
| Send only relevant context to a model | `aetron/ai_providers` | not started |

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
- [ ] Parsers for languages other than Python
- [ ] Optimised context representation
- [ ] Support for local models (Ollama, Llama, Qwen, DeepSeek)
- [ ] Support for API models (Claude, GPT, Gemini)
- [ ] Documentation generation
- [ ] Potential bug detection

## Installation

Python 3.11 or newer.

```bash
pip install -r requirements.txt
```

The only dependency is `pathspec`, and it is optional: without it Aetron falls
back to its built-in ignore rules instead of reading `.gitignore`.

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
│   ├── python_parser.py  Python, via the standard ast module
│   ├── resolver.py   imports -> edges between files
│   ├── deadcode.py   unused definitions, with confidence levels
│   └── analyzer.py   parse every file, then link them
├── context/          not started
├── ai_providers/     not started
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

<!-- CONTRIBUTING.md will be added later -->

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
