#!/usr/bin/env bash
# Symlink (or copy) this project-local skill into the local Codex skills
# directory so an agent can discover it. Safe to re-run; idempotent.
#
# Override the destination with CODEX_SKILLS_DIR. By default the skill is
# linked under ~/.codex/skills/cloudzy-deployment.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="cloudzy-deployment"
DEST_ROOT="${CODEX_SKILLS_DIR:-$HOME/.codex/skills}"
DEST_DIR="$DEST_ROOT/$SKILL_NAME"

mkdir -p "$DEST_ROOT"

if [ -L "$DEST_DIR" ] || [ -e "$DEST_DIR" ]; then
  rm -rf "$DEST_DIR"
fi

if ln -s "$SRC_DIR" "$DEST_DIR" 2>/dev/null; then
  echo "linked $DEST_DIR -> $SRC_DIR"
else
  cp -r "$SRC_DIR" "$DEST_DIR"
  echo "copied $SRC_DIR -> $DEST_DIR"
fi
