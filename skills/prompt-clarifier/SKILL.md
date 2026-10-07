---
name: prompt-clarifier
description: Turns vague, underspecified coding-task requests into precise, actionable specifications before work begins. Use this skill whenever a user's request is short or ambiguous — missing a target file/function, missing success criteria, or using a vague verb like "fix this", "make it faster", "clean this up", "add a login feature", "improve this". Propose a clearer restatement of the same request and get it confirmed before starting. Do not trigger when the request already names what to change, where, and what success looks like.
license: MIT
---

# Prompt Clarifier

## Why this matters
A vague task request forces guesswork: which file, what "faster" means, what counts as done. Guessing wrong wastes an entire work cycle — wrong file explored, wrong fix applied, more back-and-forth than if the request had been clear from the start. Clarifying up front costs one short exchange and saves everything downstream.

## When to use this skill
Trigger when a request is missing two or more of:
- **Target** — which file, function, module, or page is affected
- **Concrete change** — what should actually happen, not just a verb ("fix", "improve", "clean up")
- **Success criteria** — how to tell the work is done and correct
- **Scope/constraints** — what should NOT change

## When NOT to use it
If the request already names the target, the intended change, and how to verify it — skip this skill entirely and go straight to work. Re-clarifying an already-clear request is friction, not help.

## Process
1. Check the request against the four elements above. If it's already specific, do nothing and proceed normally.
2. Look for missing pieces in available context first (open files, error messages, repo structure, recent conversation) — many are implied by what's in front of you even when not stated outright.
3. If the **target** is still unknown after checking context, stop there: you can't honestly state success criteria or constraints for a file or page you can't identify. Ask directly and briefly for just the target, in plain language, while confirming what you *do* already understand about the intent:
   > "Which page/file do you mean? I get that you want it faster — just need to know where."
   Never fill an unknown piece with a placeholder like `[fill in]` inside a "clarified" restatement — a restatement with blanks in it isn't clarified, it's the same question wearing a costume, and it reads as broken to whoever's reading it.
4. If the target is known (stated, or inferred from context) but other details are missing, draft a full clarified restatement of the *same* request (never change what the user is asking for, only make it concrete):
   - Name the target
   - State the concrete change
   - State constraints / what must not change
   - State how success will be checked
   Show both versions and ask for confirmation:
   > You said: "<original>"
   > Here's a more specific version of the same request:
   > <clarified>
   > Proceed with this, or would you adjust anything?
5. Wait for confirmation or an edit before doing any work. If the user corrects the clarified version, use their correction, not your own guess.

## Keep it short
The clarified version should be 2-4 sentences, not a spec document. It restates intent precisely; it doesn't add new requirements the user didn't ask for.

## Examples

**Example 1**
User: "oprav to"
Clarified: "Oprav chybu v `auth.js`, kde přihlášení s platným heslem občas vrací 401 (vypadá to na race condition v ověření tokenu). Hotovo je, když existující login testy projdou a 10 opakovaných přihlášení po sobě neselže ani jednou."

**Example 2**
User: "udělej to rychlejší"
Clarified: "Sniž dobu načtení `/dashboard` pod 1 s na desktopu (teď ~3 s) — zaměř se na N+1 dotazy v `dashboardController` a zbytečné re-rendery v `DashboardView`. Nezasahuj do API kontraktu. Hotovo je, když Lighthouse performance skóre vzroste aspoň o 20 bodů."

**Example 3**
User: "přidej login"
Clarified: "Přidej přihlašovací formulář (email + heslo) na `/login`, napojený na `authService.login()`. Po úspěchu přesměruj na `/dashboard`, při chybě zobraz srozumitelnou hlášku. Hotovo je, když projde celý flow end-to-end a existuje test na neplatné přihlašovací údaje."

**Example 4 — target unknown, ask for just that**
User: "tahle stránka je pomalá, mrkni na to"
(No open file or prior context points to a specific page.)
Response: "Kterou stránku myslíš — soubor nebo route? Zbytek je jasný: chceš ji zrychlit, bez změny chování."

**Example 5 — English**
User: "fix this"
Clarified: "Fix the 401 on `POST /login` in `auth.js` when a valid password is sent (looks like a token-refresh race). Done when existing login tests pass and 10 back-to-back logins succeed."

**Example 6 — English**
User: "make it faster"
Clarified: "Cut `/dashboard` load time under 1s on desktop (currently ~3s) by fixing N+1 queries in `dashboardController` and extra re-renders in `DashboardView`. Do not change the API contract. Done when Lighthouse performance rises by at least 20 points."

**Example 7 — English**
User: "add login"
Clarified: "Add an email+password form on `/login` wired to `authService.login()`. Redirect to `/dashboard` on success; show a clear error on failure. Done when the flow works end-to-end and a test covers invalid credentials."

**Example 8 — English, target unknown**
User: "this page is slow, look at it"
(No open file or prior context points to a specific page.)
Response: "Which page — file or route? The rest is clear: you want it faster, without changing behaviour."

