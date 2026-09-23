# Contributing

Aetron is worked on by more than one agent at a time, sometimes in parallel on
the same day. This file exists because that has already gone wrong once, and
the fix is three habits rather than any tooling.

## Branch from current `main`, every time

```bash
git fetch origin
git checkout main && git pull origin main
git checkout -b feat/what-you-are-doing
```

**Pull before you start, not before you push.** A branch is a snapshot of the
moment it was cut. Work for a few hours off a stale snapshot and the conflict
you get is not with anyone's mistake — it is with everything that landed while
you were not looking.

This is what happened on 2026-09-13. Two agents worked the same afternoon:

- One branched from `779f6fd` and built the `explain` command. Good work,
  finished, 204 tests passing.
- The other branched from the same commit, built the retrieval protocol plus
  C#, JavaScript and TypeScript parsers, and merged first — 410 tests.

Neither had done anything wrong, and neither branch was broken. But the first
branch was now describing a project that no longer existed: its report said C#
was unsupported, and it had independently fixed a line-counting bug the other
branch had also fixed, differently. Four files conflicted. Merging took a
careful hour that a `git pull` at the start would have avoided.

## One branch per task, and land it before starting the next

A branch that stays open for days accumulates conflict with everything else.
Small branches merge cleanly; long ones argue. If a task turns out to be two
tasks, cut a second branch rather than growing the first.

Never commit to `main` directly, and never push to a branch another agent is
working on.

## Before you push

```bash
python -m pytest                       # must be green
pip uninstall -y pathspec && python -m pytest   # must also be green
pip install pathspec
```

Both runs matter. `pathspec` is optional at runtime, and a bug that only
appears without it has already shipped once.

The suite includes `tests/test_no_secrets.py`, which fails if anything shaped
like an API key is tracked or about to be. If it fires, do not weaken it:
remove the key, revoke it with its provider, and read it from an environment
variable. A test that needs a fake key builds it at runtime, so no key-shaped
literal is ever committed.

Then rebase or merge `main` in and run the suite again, so the conflict is
yours to resolve rather than the next person's:

```bash
git fetch origin && git merge origin/main
```

## If you find a bug outside your task

Fix it, and say so in the commit message — but keep the fix in the same commit
as its regression test, so someone reading the history later can tell what the
bug actually was. Do not leave it for whoever comes next; two agents
independently fixing the same bug is worse than either fixing it alone.

## Two rules the code depends on

Both are explained at length in `CLAUDE.md`; they are repeated here because
breaking either has broken the tool before.

**Nothing is dropped silently.** Every skipped file carries a reason, every
pruned directory is listed. A report that quietly omits source cannot be
trusted with an unfamiliar project — which is why `explain` ends with what the
analysis could *not* cover.

**No layer knows about the layer above it.** The scanner returns data and never
prints. The analyzer takes a scan result and returns an index. The CLI is one
consumer, a model is another, a GUI would be a third.

## Style

Prose docstrings explaining *why* a module exists, not what it does. Comments
record the finding that forced a decision — several in this codebase were
written after a real false positive, and they are the reason nobody has undone
those decisions since.
