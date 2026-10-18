#!/usr/bin/env bash
# Run a command as the lifetracker user on the production Fly Machine, via
# ./flyctl.sh (so FLY_APP comes from .env), e.g.
# `./flyssh.sh python manage.py showmigrations` or `./flyssh.sh ls -l /data`.
# With no arguments, starts an interactive bash. Drops privileges with setpriv,
# as docker-entrypoint.sh does, rather than `su -`, so the command keeps the
# Machine's environment (LIFETRACKER_DATA_DIR, the venv on PATH, Fly secrets),
# and starts in the app directory. Arguments are run directly, not through a
# shell, each quoted so one containing spaces or quotes arrives intact; use
# `./flyssh.sh bash -c '...'` for shell syntax. Passes --pty only when stdin
# and stdout are both terminals, so piped or redirected use works too. Starts
# the Machine first if it's stopped, as SSH doesn't (needs jq).
set -euo pipefail

dir="$(dirname "$0")"

# Quote $1 as a single POSIX shell word: wrap it in single quotes, with each
# embedded ' written as '\''.
squote() {
  local rest="$1" quoted=""
  while [[ "$rest" == *"'"* ]]; do
    quoted+="${rest%%"'"*}'\\''"
    rest="${rest#*"'"}"
  done
  printf "'%s'" "$quoted$rest"
}

if [ "$#" -eq 0 ]; then
  # Not `bash -l`: Debian's /etc/profile resets PATH for non-root users,
  # dropping the venv.
  set -- bash
fi

# setpriv leaves HOME as /root, which lifetracker can't write to (see
# docker-entrypoint.sh). The SSH session doesn't set USER or LOGNAME, so set
# them too, for tools that expect them. sh only changes directory; the command
# is then exec'd from its own arguments, not parsed by it.
command='env HOME=/home/lifetracker USER=lifetracker LOGNAME=lifetracker setpriv --reuid=lifetracker --regid=lifetracker --init-groups'
command+=' sh -c '"$(squote 'cd "${LIFETRACKER_APP_DIR:-/app}" && exec "$@"')"' sh'
for arg in "$@"; do
  command+=" $(squote "$arg")"
done

pty=()
if [ -t 0 ] && [ -t 1 ]; then
  pty=(--pty)
fi

# The app runs only one Machine (see README.md's Fly.io section), as in
# fly-machine-id.sh. Progress messages go to stderr, keeping stdout for the
# command's output.
machine="$("$dir/flyctl.sh" machine list --json \
  | jq -r '.[0] // error("the app has no Fly Machine") | "\(.id) \(.state)"')"
id="${machine%% *}"
state="${machine#* }"
if [ "$state" != started ]; then
  echo "flyssh.sh: starting Machine $id (it's $state)" >&2
  "$dir/flyctl.sh" machine start "$id" >&2
  "$dir/flyctl.sh" machine wait "$id" --state started >&2
fi

exec "$dir/flyctl.sh" ssh console --machine "$id" ${pty[@]+"${pty[@]}"} \
  --command="$command"
