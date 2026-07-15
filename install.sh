#!/usr/bin/env bash
# local0 — one-line installer.
#   curl -fsSL https://raw.githubusercontent.com/<owner>/local0/main/install.sh | bash
# Clones the repo, then runs `make quickstart` (pull models → up → ingest).
# Read before piping to bash: https://github.com/<owner>/local0/blob/main/install.sh
set -euo pipefail

REPO="${LOCAL0_REPO:-https://github.com/<owner>/local0.git}"   # override: LOCAL0_REPO=...
DIR="${LOCAL0_DIR:-local0}"                                    # override: LOCAL0_DIR=...

say() { printf '\033[36m▸ %s\033[0m\n' "$1"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$1" >&2; exit 1; }

# ponytail: prereq checks only — make quickstart does the actual setup.
command -v git    >/dev/null || die "git not found"
command -v docker >/dev/null || die "docker not found — install Docker Desktop / Engine"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not found"
command -v ollama >/dev/null || die "ollama not found — install from https://ollama.com and run 'ollama serve'"
curl -fsS http://localhost:11434/api/version >/dev/null 2>&1 \
  || die "Ollama not reachable on :11434 — start it with 'ollama serve'"

if [ -d "$DIR/.git" ]; then
  say "Updating existing checkout in ./$DIR"
  git -C "$DIR" pull --ff-only
else
  [ -e "$DIR" ] && die "./$DIR exists and is not a git checkout — set LOCAL0_DIR=other"
  say "Cloning $REPO → ./$DIR"
  git clone --depth 1 "$REPO" "$DIR"
fi

say "Running 'make quickstart' (pull models → start → ingest)…"
make -C "$DIR" quickstart

printf '\n\033[32m✓ Router ready → http://localhost:8081/dashboard\033[0m\n'
