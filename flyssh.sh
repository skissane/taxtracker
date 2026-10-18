#!/usr/bin/env bash
# Open a shell as the lifetracker user on the production Fly Machine, via
# ./flyctl.sh (so FLY_APP comes from .env). With no arguments, starts an
# interactive login shell; with arguments, runs them as one command in a login
# shell, e.g. `./flyssh.sh python manage.py showmigrations` or
# `./flyssh.sh ls -l /data`. Each argument is quoted, so one containing spaces
# or quotes reaches the command intact. Passes --pty only when stdin and stdout
# are both terminals, so piped or redirected use works too.
set -euo pipefail

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

su_command="su -s /bin/bash"
if [ "$#" -gt 0 ]; then
  # The inner command, for bash -c, is each argument quoted; it's then quoted
  # again as a whole so it reaches su as a single -c argument.
  inner=""
  for arg in "$@"; do
    inner+="${inner:+ }$(squote "$arg")"
  done
  su_command+=" -c $(squote "$inner")"
fi
su_command+=" - lifetracker"

pty=()
if [ -t 0 ] && [ -t 1 ]; then
  pty=(--pty)
fi

exec "$(dirname "$0")/flyctl.sh" ssh console ${pty[@]+"${pty[@]}"} --command="$su_command"
