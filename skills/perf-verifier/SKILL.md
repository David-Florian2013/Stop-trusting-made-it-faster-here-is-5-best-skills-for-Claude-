---
name: perf-verifier
description: Makes performance work evidence-based instead of guesswork. Use whenever the task is to make code faster or lighter (slow endpoint, query, script, build, page, memory use), and when writing code that loops over data, queries a database, calls APIs or handles large inputs. Profiles to find the real bottleneck, measures before/after with a statistical test, checks the output did not change, and shows how runtime grows with input size. Never claim a speedup without a measurement, even if the user only said "optimize this".
license: MIT
compatibility: Requires Python 3 on Linux or macOS. git needed for before/after checks.
---

# Perf Verifier

Language models write code that is slower than expert code (about 3x the run time of reference solutions in one benchmark), and coding agents optimising real repositories fail in three documented ways: they optimise something that is not the bottleneck, they stop after the first patch that passes, and they do not test enough, so results change. Speedup claims are also unreliable: in a 30-run study with a Mann-Whitney test, only about 6% of solutions labelled "faster" were measurably faster. This skill gives you numbers to act on instead of a feeling.

`PF` means `python3 <this skill's directory>/scripts/pf.py`. It needs only Python 3 (Linux or macOS). `--cmd` takes the command as one quoted string; everything after a bare `--` is also read as the command.

## Workflow
1. **Is it slow?** `PF bench --cmd 'python3 job.py 5000'` gives median time and peak memory. Pick an input of realistic size; a command under 30 ms measures process start-up, not your code.
2. **Where does the time go?** `PF profile -- python3 job.py 5000` (Python or Node commands). Rows marked `*` are your code. Work on the top `*` row. The `ceiling` line says the most the whole run can speed up if that function became free: if the ceiling is smaller than the speedup you need, the bottleneck is elsewhere or the algorithm must change.
3. **Prefer the algorithm.** `PF scan` lists slow patterns in the lines you changed (query or request inside a loop, list used for membership, `await` in a loop, copying an accumulator, blocking calls in async code, and more). Treat each finding as a hypothesis: act on it only if it sits on the profiled hot path. Findings are about patterns and cannot know the size of your data.
4. **Check how it scales.** `PF scale --sizes 2000,4000,8000,16000 --cmd 'python3 job.py {N}'` fits time against input size and names the growth (linear, about quadratic, ...). A change that only helps at one size can be a measurement artefact; a quadratic that became linear is real.
5. **Prove the change.** After editing, with the old version still in git: `PF check --cmd 'python3 job.py 5000' --expect faster --verify '<the project test command>'`. It runs the committed version and your working tree alternately, tests the difference, compares the program output, and runs the test command. Use `--base <ref>` to compare against another commit, or `--a 'old command' --b 'new command'` to compare two implementations without git.
6. **Do not stop at the first win.** Profile again. Stop when the top ceiling is under about 1.1x or the target is met.
7. **Report.** Print the `SUMMARY:` line from step 5 as the last line of your reply. If you measured nothing, say so in one sentence.

## Reading the verdict
- `FASTER` / `SLOWER`: the difference passed the test (p < 0.05) and exceeded the measured noise. Only `FASTER` supports saying "faster".
- `NO MEASURABLE DIFFERENCE`: the change did nothing you can show. Say that, and consider reverting if the new code is more complex.
- `OUTPUT DIFFERENT`: the two versions print different results. This is a failure, not a speedup. `nondeterministic` means output varies between runs of the same code, so equality was not verified; use `--verify` with real tests.
- `NOT PROVEN` (with `--expect faster`) and `UNMEASURED` mean no claim of speedup is allowed.
- A command that fails or times out cannot be timed; fix it first.

## Things to know
- The before-version is the committed files only. If the command needs untracked data or generated files, pass `--link path` (repeatable) so they are linked into the before-copy. `node_modules`, `.venv`, `venv` are linked automatically.
- `compare` takes about a minute at most (`--budget` seconds, `--runs` to fix the count). Few runs means low power and the output says so.
- `profile` supports `python`/`node` commands only; for other stacks use that stack's profiler and still use `compare` and `scale`, which work with any command.
- To silence a scan finding that is a false positive: `# pf: ignore RULE-CODE` (or `// pf: ignore RULE-CODE`) on the line or the line above.
- Memory: `bench`, `compare` and `scale` report peak memory of the command; a speedup bought with much more memory is worth stating.
