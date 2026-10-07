---
name: repo-map
description: Builds and maintains a lightweight architecture map of the current repository (folder structure, entry points, key dependencies) so the assistant doesn't have to re-explore the whole codebase on every task. Use this skill when starting work in a repository that has no `.claude/MAP.md` yet, or when the map is stale. Consult an existing map first before manually exploring an unfamiliar repo.
license: MIT
---

# Repo Map

## Why this matters
Exploring a repo's structure from scratch on every task burns tool calls and tokens re-discovering things that don't change between tasks — which folder holds what, where the entry points are, how modules depend on each other. A cached map turns repeated exploration into a single read.

## When to use
- No `.claude/MAP.md` exists yet in the repo root
- MAP.md exists but a **structural** change has landed since it was last updated (see staleness check below)
- Starting a non-trivial task in an unfamiliar repo — check for a map before manually exploring

## Process
1. Check for `.claude/MAP.md`.
2. If absent, survey the repo: top-level folders, README, package manifest, existing docs. Identify entry points (main/index files, app bootstrap, primary routes). Note the purpose of each top-level folder in one line. Note key dependencies between major modules (what calls what, not a full graph).
3. Write the map using the template below to `.claude/MAP.md`, recording the current git commit hash.
4. If MAP.md exists, run the staleness check below. If it says stale, apply the smallest edit that fixes it — don't rewrite the whole file unless told to do a full rebuild.
5. On any task, read MAP.md first instead of re-exploring; only dig into files the map points at that are directly relevant to the task.

## Staleness check
A raw "did the commit hash change" check is too eager — nearly every session adds *some* commit, and re-surveying the repo on every trivial change burns exactly the tokens this skill exists to save. Check structure, not commits:

1. List what actually changed since the map's recorded commit, ignoring the map itself: `git diff --name-only <recorded-hash> HEAD -- . ':!.claude/MAP.md'`. (No git history available? Compare the current folder listing against the Structure section instead.)
2. The map is stale only if a changed path introduces or removes a **folder at the level the Structure section lists** (e.g. a new `src/<name>/` when the map lists `src/routes/`, `src/services/`) that isn't already named there, or touches a file already listed as an **entry point**. A change confined to files inside a folder the map already describes doesn't make the map wrong — it's still accurate at the level the map operates on, so treat it as fresh and do nothing.
3. When it is stale, edit just the affected bullet(s) — add or remove the one line for the new/removed folder, or re-check the one entry point that changed. Reserve a full rebuild for when several top-level folders have shifted at once or the file no longer reflects the repo at a glance.
4. Update the recorded commit hash after any edit, incremental or full.

## Keep the map small
The map only pays off if reading it costs far less than exploring. Keep it under about 40 lines: one line per folder, no per-file listings, no restating the README. If it grows past that, cut detail instead of adding sections.

## Template
```
# Repo Map
Last updated: <date>, commit <hash>

## Structure
- `src/` — ...
- `tests/` — ...

## Entry points
- `src/index.js` — app bootstrap

## Key dependencies
- `routes/` call into `services/`, which call `db/`
```

## Examples

**Example 1 — no map yet**
Repo has no `.claude/MAP.md`. Survey it, write the map fresh with the current commit hash.

**Example 2 — trivial change, map stays fresh**
Since the map's recorded commit, the only change is a new function added inside `src/services/orderService.js` — a file already covered by the `src/services/` bullet. No new folder at the listed level, no entry-point file touched. The map is still accurate; do nothing.

**Example 3 — structural change, incremental update**
Since the map's recorded commit, a new `src/jobs/` folder appeared with a scheduled-job file. The map lists `src/routes/`, `src/services/`, `src/db/`, so this is a new folder at the listed level, and it isn't in the map. Add one bullet — `` `src/jobs/` — scheduled background jobs `` — under Structure, update the recorded commit hash, and leave the rest of the file untouched.
