# Contributing

This repo is five skills. New skills are out of scope.

## What to send

- A **receipt** (`pf.py` / `vf.py` / `cs.py` output, or the prompt that misfired).
- English. Czech examples in `prompt-clarifier` can stay; public docs stay English.
- One change per PR.

## Bugs that are actually bugs

- Wrong verdict (`FASTER` when output changed, `PASS` when tests did not run).
- Crash / traceback on Linux or macOS with Python 3.
- SKILL.md telling the agent to run a flag the script does not have.
- Install path that puts this git repo inside `~/.claude/skills/<one-skill>/`.

## What will get closed

- "Add Windows support" without a patch (known gap: `perf-verifier` uses `os.wait4`).
- "Rewrite the JS scanner as an AST" — real, not a first issue.
- Kitchen-sink skill requests. File a **skill request** issue if the claim cannot be proved by the existing three scripts.

## Dev loop

```bash
python3 skills/perf-verifier/scripts/pf.py -h
python3 skills/code-verifier/scripts/vf.py -h
python3 skills/context-saver/scripts/cs.py -h
python3 scripts/package-skills.py
```

Keep `skills/<name>/SKILL.md` as the source of truth. Regenerated `.skill` zips go in `packaged/`. Archive members must be `<name>/SKILL.md`, never a nested `<name>/<name>/`.
