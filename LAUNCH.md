# 48-hour launch notes

Copy-paste kit. **Do not buy stars.** GitHub bans fake accounts and star markets.

Do not post until a stranger can install in 30 seconds (`npx skills add` or `/plugin marketplace add`).

## Hour 0 — GitHub

Already done in-repo: unpacked `skills/`, `.skill` downloads, plugin marketplace, README, MIT, CI, issue templates, `v1.0.0` release.

Still on you:

1. **Settings → General → Social preview** — upload [`docs/social-preview.jpg`](docs/social-preview.jpg) (1280×640). GitHub does not take this from the README.
2. Confirm your HN account can submit **Show HN** (new accounts have been blocked at times).
3. Stay at the keyboard for the first 12 hours of comments.

## Hour 2–8 — Reddit (do not crosspost the same text)

**r/ClaudeCode first** (this is the right room). Disclose you are the author.

Title:

```
Skill: make Claude Code print FASTER or FAIL instead of "done" (local Python, no MCP)
```

Then **r/ClaudeAI** if your account meets the sub's karma/age floor. Frame: problem → what you tried → what you learned → link. No "5 best".

**Skip r/programming** as a launch post ("I made this" is against the rules).

**r/LocalLLaMA** only if you say plainly: the agent is Claude Code; the checkers are local stdlib Python and call no cloud API. If they remove it, do not repost.

## Hour 8–20 — X

Eight tweets, one thread. Screenshot a real `NO MEASURABLE DIFFERENCE` or `VERDICT: FAIL` receipt — not a fake 10x.

1. Claude Code told me a patch was 10x faster. I timed both versions. It wasn't. So I made the agent print a verdict from a local script before it is allowed to say "faster" or "tests pass".
2. Five skills. Three of them are Python 3 programs with zero pip deps. They run on your machine. No extra cloud call. perf-verifier / code-verifier / context-saver / repo-map / prompt-clarifier
3. perf-verifier (`pf.py`): profile Python/Node, A/B working tree vs HEAD, check stdout did not change, fit time against input size. Last line is FASTER, NO MEASURABLE DIFFERENCE, or OUTPUT DIFFERENT.
4. code-verifier (`vf.py`) receipt: syntax, imports, scan, tamper, tests, lint, types. `tamper` catches weakened assertions, skipped tests, and hard-coded expected values.
5. The other three: context-saver peeks one function; repo-map writes a 40-line `.claude/MAP.md`; prompt-clarifier restates "make it faster" and waits.
6. `vf.py` treats "zero tests collected" as FAIL, not skip.
7. Limits: Linux/macOS, stdlib Python 3, JS/TS scan is patterns not AST, `profile` is Python/Node, a clean receipt is not a security audit.
8. MIT. `npx skills add David-Florian2013/Stop-trusting-made-it-faster-here-is-5-best-skills-for-Claude-` — plus the GitHub URL.

Do not tag `@ClaudeDevs` begging for a retweet.

## Hour 20–30 — Show HN (Tue–Thu, ~8–10am ET)

Title:

```
Show HN: Claude Code skills that refuse "faster" and "tests pass" without local proof
```

Link the repo. First comment in 60 seconds: what it is, how to install in one command, the limits, no adjectives. **Do not ask friends to upvote.** HN bans that. HN also dislikes AI-edited text — paste the comment yourself.

Show HN of skill *dumps* dies. This works only because it is a sharp claim plus something to run.

## After ~10 real stars — awesome lists

One list per day, one-line blurb, no emoji:

```
perf-verifier, code-verifier, context-saver, repo-map, and prompt-clarifier — Claude Code skills that benchmark speedups, run the project's tests, and flag weakened assertions using local Python 3 (stdlib only).
```

Reasonable targets: [travisvn/awesome-claude-skills](https://github.com/travisvn/awesome-claude-skills), [VoltAgent/awesome-agent-skills](https://github.com/VoltAgent/awesome-agent-skills), [karanb192/awesome-claude-skills](https://github.com/karanb192/awesome-claude-skills).

Wait on [hesreallyhim/awesome-claude-code](https://github.com/hesreallyhim/awesome-claude-code): resource must be ≥14 days old with commits after day 1, **or** ≥100 stars. Issue form only, not a PR. Earliest date from first commit: **2026-10-20**.

## Do not

- Buy stars or join star-for-star groups.
- Post identical copy to five subs in one hour.
- Put fake user counts or "10x" on the social preview.
- Open Product Hunt for a skill catalog (2026 featuring rules exclude directories/templates).
