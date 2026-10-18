#!/usr/bin/env bash
# Run flyctl against the production app named by FLY_APP in the repo root's
# .env, e.g. `./flyctl.sh status` or `./flyctl.sh logs`, so the app's name
# needn't be exported in every shell (see README.md's Fly.io section). Exports
# FLY_APP from .env, overriding any already in the environment, then execs
# flyctl with this script's arguments. Fails if .env is missing, or doesn't set
# FLY_APP to a non-blank value.
set -euo pipefail

env_file="$(dirname "$0")/.env"

if [ ! -f "$env_file" ]; then
  echo "error: $env_file not found" >&2
  exit 1
fi

# Last FLY_APP assignment in .env, or empty. Parsed as in fly-setup.sh's
# dotenv_get: .env is dotenv, not shell, so read it rather than source it.
FLY_APP="$(
  { grep -E '^[[:space:]]*(export[[:space:]]+)?FLY_APP[[:space:]]*=' "$env_file" || true; } | tail -n 1 |
    sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
)"

if [ -z "$FLY_APP" ]; then
  echo "error: FLY_APP is missing or empty in $env_file" >&2
  exit 1
fi

export FLY_APP
exec flyctl "$@"
