#!/usr/bin/env bash
# One-time setup of the production Fly.io app (see README.md's Fly.io section).
# Reads FLY_APP, LIFETRACKER_INITIAL_ADMIN_PASSWORD and the
# LIFETRACKER_BACKUP_S3_* settings from ./.env, then creates whatever is
# missing: the Fly app, its volume, those settings as Fly secrets, and the
# FLY_APP and FLY_API_TOKEN GitHub Actions secrets (the latter a new deploy
# token scoped to the app). Safe to re-run: it never changes anything that
# already exists, so to change a value, use `fly secrets set` / `gh secret set`.
#
# The exception is --replace-token, which always creates a new deploy token
# and overwrites FLY_API_TOKEN with it, e.g. after moving to a new app (an
# existing token is scoped to the old one) or to rotate it. It doesn't revoke
# the old token; see `fly tokens list -a <app>` and `fly tokens revoke`.
set -euo pipefail
cd "$(dirname "$0")"

replace_token=false
for arg in "$@"; do
  case "$arg" in
    --replace-token) replace_token=true ;;
    *) echo "usage: $0 [--replace-token]" >&2; exit 2 ;;
  esac
done

# Set on the app as Fly secrets. The admin password only takes effect when
# ensure_superuser first creates `admin`, on the first boot.
fly_secrets=(
  LIFETRACKER_INITIAL_ADMIN_PASSWORD
  LIFETRACKER_BACKUP_S3_ENDPOINT_URL
  LIFETRACKER_BACKUP_S3_BUCKET_NAME
  LIFETRACKER_BACKUP_S3_REGION_NAME
  LIFETRACKER_BACKUP_S3_ACCESS_KEY_ID
  LIFETRACKER_BACKUP_S3_SECRET_ACCESS_KEY
)

for cmd in fly gh jq; do
  command -v "$cmd" >/dev/null || { echo "error: $cmd is not installed" >&2; exit 1; }
done

if [ ! -f .env ]; then
  echo "error: .env not found in the repo root" >&2
  exit 1
fi

# Value of $1 in .env (last assignment wins), or empty. .env is dotenv, not
# shell (uv parses it without executing it), so read it rather than source it.
# Handles NAME=value, an optional `export `, and one pair of surrounding quotes;
# not multi-line values or ${VAR} interpolation.
dotenv_get() {
  { grep -E "^[[:space:]]*(export[[:space:]]+)?$1[[:space:]]*=" .env || true; } | tail -n 1 |
    sed -E 's/^[^=]*=[[:space:]]*//; s/[[:space:]]+$//; s/^"(.*)"$/\1/; s/^'"'"'(.*)'"'"'$/\1/'
}

missing=()
for name in FLY_APP "${fly_secrets[@]}"; do
  value=$(dotenv_get "$name")
  [ -n "$value" ] || missing+=("$name")
  printf -v "$name" '%s' "$value"
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "error: missing or empty in .env: ${missing[*]}" >&2
  exit 1
fi

# The volume's name and region come from fly.toml, so they can't drift from it.
region=$(sed -nE 's/^primary_region[[:space:]]*=[[:space:]]*"(.*)"/\1/p' fly.toml)
volume=$(sed -nE 's/^[[:space:]]*source[[:space:]]*=[[:space:]]*"(.*)"/\1/p' fly.toml)
if [ -z "$region" ] || [ -z "$volume" ]; then
  echo "error: couldn't read primary_region or [mounts] source from fly.toml" >&2
  exit 1
fi

if fly apps list --json | jq -e --arg n "$FLY_APP" 'any(.[]; .Name == $n)' >/dev/null; then
  echo "Fly app: already exists"
else
  echo "Fly app: creating"
  fly apps create "$FLY_APP"
fi

if fly volumes list -a "$FLY_APP" --json |
  jq -e --arg n "$volume" 'any((. // [])[]; .name == $n)' >/dev/null; then
  echo "Volume $volume: already exists"
else
  echo "Volume $volume: creating in $region"
  # --yes accepts the warning that a single volume has no redundancy; the app
  # can only ever run one Machine (see fly.toml).
  fly volumes create "$volume" -a "$FLY_APP" --region "$region" --size 1 --yes
fi

existing_secrets=$(fly secrets list -a "$FLY_APP" --json | jq -r '(. // [])[].name')
to_import=""
for name in "${fly_secrets[@]}"; do
  if grep -qx "$name" <<<"$existing_secrets"; then
    echo "Fly secret $name: already set"
  else
    echo "Fly secret $name: setting"
    to_import+="$name=${!name}"$'\n'
  fi
done
# Via stdin rather than `fly secrets set NAME=value`, so the values never
# appear in the process list.
if [ -n "$to_import" ]; then
  printf '%s' "$to_import" | fly secrets import -a "$FLY_APP"
fi

github_secrets=$(gh secret list --json name --jq '.[].name')

if grep -qx FLY_APP <<<"$github_secrets"; then
  echo "GitHub secret FLY_APP: already set"
else
  echo "GitHub secret FLY_APP: setting"
  printf '%s' "$FLY_APP" | gh secret set FLY_APP
fi

if $replace_token || ! grep -qx FLY_API_TOKEN <<<"$github_secrets"; then
  echo "GitHub secret FLY_API_TOKEN: setting to a new deploy token"
  # Into a variable first rather than piped straight to gh, which would store
  # an empty secret if fly failed. Named so it's recognisable in `fly tokens
  # list`.
  token=$(fly tokens create deploy -a "$FLY_APP" --name "GitHub Actions")
  if [ -z "$token" ]; then
    echo "error: fly tokens create deploy printed no token" >&2
    exit 1
  fi
  # The same check CI's check-fly job makes, so a token that would fail it is
  # never stored. Via the environment rather than --access-token, to keep it
  # out of the process list; FLY_ACCESS_TOKEN too, as it takes precedence
  # over FLY_API_TOKEN if already set.
  if ! FLY_ACCESS_TOKEN="$token" FLY_API_TOKEN="$token" \
    fly status -a "$FLY_APP" >/dev/null; then
    echo "error: fly status failed with the new deploy token, so FLY_API_TOKEN" \
      "was not set; revoke the token with \`fly tokens revoke\`" >&2
    exit 1
  fi
  printf '%s' "$token" | gh secret set FLY_API_TOKEN
else
  echo "GitHub secret FLY_API_TOKEN: already set (--replace-token to replace)"
fi
