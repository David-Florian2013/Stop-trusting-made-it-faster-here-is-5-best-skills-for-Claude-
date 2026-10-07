---
name: context-saver
description: Cuts the tokens spent reading files and command output without losing anything the task needs. Use on any coding or repo task that involves reading source files longer than about 150 lines, running tests, builds, installs, git log or other commands that print more than about 60 lines, or searching a codebase. Shows only the needed part of a file (by function name, pattern or line range), shortens long command output while keeping every error, and prints the tokens saved at the end of the task. Use it proactively on every such task, even if the user never mentions tokens or limits.
license: MIT
compatibility: Requires Python 3.
---

# Context Saver

Tokens spent reading irrelevant text are unavailable for the task and count against the user's limit. Look at the smallest slice that is enough, and widen the moment it is not. A saving that produces a wrong answer is not a saving.

`CS` below means `python3 <this skill's directory>/scripts/cs.py`. It needs only Python 3.

## Reading files
1. If `.claude/MAP.md` exists, read it before exploring.
2. File under about 150 lines: read it whole. Slicing a small file saves nothing.
3. Larger file: run `CS outline FILE` (functions, classes or headings with line ranges), then `CS peek FILE --symbol NAME`. For text instead of a name use `--grep REGEX` (`-i` ignores case, `--context N` widens each match). For a known range use `--lines A-B`. If the output says the block end was not detected, widen with `--lines`.
4. Before editing, view the exact lines you will change with `peek --lines`, so the edit matches the file and not your memory of it.
5. Do not re-read what you have already seen unless the file changed.

## Command output
First ask the command for less: `git log --oneline -20`, `pytest -q`. If it may still print more than about 60 lines (verbose tests, builds, installs, log files), use `COMMAND 2>&1 | CS squeeze`. It keeps the first and last lines, every line mentioning an error, failure, warning or non-zero exit with 2 lines around it, and collapses repeated lines. The last line names a file holding the complete output.

- `--keep REGEX` also protects lines that matter for this task, such as a test name or an id.
- If lines were omitted and the answer could be among them, do not guess: `CS peek <that file> --grep REGEX`.
- Do not squeeze output the user asked to see, or search results (`grep -r`, `find`); narrow the search until the results are complete.

## Never shorten
Error messages and stack traces, code you are about to change, anything the user asked to see verbatim.

## Report
As the last step of the task run `CS report --reset` and print its line as the final line of your reply. If it prints nothing, print nothing. The figures are estimates (characters divided by 4) against reading every source in full.
