#!/usr/bin/env bash
# Copy one or all skills into ~/.claude/skills (override with CLAUDE_SKILLS_DIR).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
ALL=(perf-verifier code-verifier context-saver repo-map prompt-clarifier)

if [ "$#" -gt 0 ]; then
  SKILLS=("$@")
else
  SKILLS=("${ALL[@]}")
fi

mkdir -p "$DEST"
for s in "${SKILLS[@]}"; do
  if [ ! -f "$ROOT/skills/$s/SKILL.md" ]; then
    echo "unknown skill: $s" >&2
    echo "known: ${ALL[*]}" >&2
    exit 1
  fi
  rm -rf "$DEST/$s"
  cp -R "$ROOT/skills/$s" "$DEST/$s"
  echo "installed $s -> $DEST/$s"
done
