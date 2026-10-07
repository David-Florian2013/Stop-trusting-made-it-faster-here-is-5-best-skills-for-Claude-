---
name: code-verifier
description: Verifies code Claude just wrote or edited actually works, is secure, and wasn't declared "done" on faith. Use after writing or editing code, and before telling the user a task is complete, a bug is fixed, or tests pass. Runs the project's real tests/lint/types, scans the diff for the security and correctness bugs LLM-written code is documented to make most often (injection, XSS, unawaited promises, mutable defaults, secrets, invented packages), and checks whether the diff weakened, deleted, or special-cased a test to fake a pass. Use on every task that touches code, even when the user didn't ask for a review.
license: MIT
compatibility: Requires Python 3. Optional node on PATH for .js/.mjs/.cjs syntax checks.
---

# Code Verifier

Claude's own judgment that code "looks right" is not evidence it works. Published research on LLM-written code backs this up directly, not as a hypothetical: independent testing across 100+ models found close to half of AI-generated code samples contain an OWASP Top-10 vulnerability, with cross-site scripting and log injection failing well over half the time; separate studies put invented ("hallucinated") package names at roughly 5% for commercial models; and controlled studies of coding agents have caught models passing a test by weakening its assertion, deleting it, or hard-coding the exact value it checks, rather than by fixing the underlying bug. None of that shows in code that "looks done." This skill closes the gap: it gives you something to run, not just something to feel confident about.

`VF` below means `python3 <this skill's directory>/scripts/vf.py`. It needs only Python 3; a `node` binary on PATH additionally enables JS/TS syntax checking.

## Process
1. Finish the code change.
2. Run `VF check` from inside the repo. With no arguments it checks exactly what changed since `HEAD` (staged, unstaged, and untracked files) — the same scope a reviewer would look at. Pass paths to check specific files instead, or `--base <ref>` (a commit, branch, or tag) to check everything changed since some earlier point, e.g. `--base main` for a whole PR.
3. Read the receipt line by line. Each row is `syntax`, `imports`, `scan`, `tamper`, `tests`, `lint`, `types` — `OK`/`n/a` (fine), `FAIL` (a real problem), `REVIEW` (needs a written explanation, not a fix), or `NOT RUN` (nothing was verified for that row — say so, don't imply it passed).
4. Fix every `FAIL` and re-run `VF check` until it's gone. Do not tell the user the task is done, a bug is fixed, or tests pass while VERDICT starts with `FAIL`.
5. If VERDICT is `REVIEW`, explain the flagged item in your reply — say why the test change is legitimate (e.g. "the old assertion was wrong because...") or fix it. Silently proceeding past a `tamper` REVIEW is the exact failure mode this skill exists to catch, including in your own edits.
6. If VERDICT is `PARTIAL` (tests didn't run — no test framework detected, or none applied to the changed files), tell the user plainly that correctness was not verified, instead of implying it was.
7. If nothing needs fixing, VERDICT is `PASS`. Mention it briefly; don't paste the whole receipt into the reply.

## The individual commands
Use these for a narrower look, or when `check`'s scope doesn't fit:
- `VF scan [paths]` — security/correctness patterns only (see below). `--low` includes minor style findings too.
- `VF imports [paths]` — every import/require that doesn't resolve: not stdlib, not in the project, not installed, not declared. This is what catches an invented package name before it ships. `--online` also checks the real PyPI/npm registry, distinguishing "real package, just not added yet" from "doesn't exist anywhere."
- `VF tamper [--base REF]` — compares the diff's test files and CI config against the base ref for weakened assertions, deleted tests, new skip markers, and loosened CI thresholds; also flags code that returns a literal matching one specific test's exact call.
- `VF run [--only tests|lint|types]` — just runs the project's own tooling (pytest/npm test, ruff/eslint, mypy/tsc) and reports pass/fail. A run that collects zero tests is reported as `FAIL`, not skipped as a pass — "no tests ran" is not evidence the code works.

## What `scan` looks for
Python (AST-based) and JavaScript/TypeScript (pattern-based): SQL/command/template injection built from string concatenation, XSS (innerHTML, dangerouslySetInnerHTML, unescaped template response, mark_safe/autoescape off), path traversal from request data, SSRF, insecure deserialization (pickle, unsafe yaml.load), hardcoded secrets, weak hashes on passwords, non-crypto randomness for tokens, disabled TLS verification, mutable default arguments, bare/swallowed exceptions, `is` compared to a literal, off-by-one loop bounds, unawaited promises/coroutines, and a few more. Only high/medium findings show by default; `--low` adds style-level ones like `==` vs `===`.

## Suppressing a specific finding
Add `# vf: ignore RULE-CODE` (or `// vf: ignore RULE-CODE`) on the flagged line or the line above, when a finding is a genuine false positive for this line. Don't suppress a whole file or use a bare `# vf: ignore` to silence something you haven't actually looked at.

## Limits
This is a fast heuristic scanner, not a substitute for a real SAST tool or a human security review on anything sensitive — it will miss vulnerabilities that don't match its patterns, and `tamper`'s test-weakening check only works inside a git repository with a base commit to compare against. Treat a clean `VF check` as "no known red flags," not as a certificate.
