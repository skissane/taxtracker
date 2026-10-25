#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Outside a Git checkout (e.g. a `git archive` copy) there's nothing to set up,
# and failing would make every `just` recipe fail.
if ! toplevel="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    exit 0
fi
cd "$toplevel"

ensure_setting() {
    local name="$1" value="$2"
    local current
    current="$(git config --get --local "$name" || test $? -eq 1)"
    if test "$current" != "$value"; then
        git config --local "$name" "$value"
        echo >&2 "INFO: Updated local repo git config setting '$name' to '$value'"
    fi
}

ensure_setting "blame.ignoreRevsFile" ".git-blame-ignore-revs"
