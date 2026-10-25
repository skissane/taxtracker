#!/usr/bin/env bash
# Sets this repo's local Git config settings that the project relies on, so
# nobody has to set them up by hand. The justfile runs it whenever `just` runs a
# recipe, via a top-level `shell()` assignment; it's idempotent and only prints
# (to stderr) when it changes a setting. Currently it sets only
# `blame.ignoreRevsFile`, so `git blame` skips the commits listed in
# .git-blame-ignore-revs.
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

# Git 2.52 added the ":(optional)" prefix, so `git blame` doesn't fail on a
# checkout from before .git-blame-ignore-revs existed, but 2.52.0's blame reads
# an uninitialised pointer when the file is missing (fixed in 2.53.0). Older
# Git would take the prefix as part of the file name.
ignore_revs_file=".git-blame-ignore-revs"
if [[ "$(git version)" =~ ^git\ version\ ([0-9]+)\.([0-9]+) ]] &&
    (( BASH_REMATCH[1] > 2 || (BASH_REMATCH[1] == 2 && BASH_REMATCH[2] >= 53) )); then
    ignore_revs_file=":(optional)$ignore_revs_file"
fi

ensure_setting "blame.ignoreRevsFile" "$ignore_revs_file"
