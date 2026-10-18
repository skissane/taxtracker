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
# and stdout are both terminals, so piped or redirected use works too.
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

if [ "$#" -eq 0 ]; then
  # Not `bash -l`: Debian's /etc/profile resets PATH for non-root users,
  # dropping the venv.
  set -- bash
fi

# setpriv leaves HOME as /root, which lifetracker can't write to (see
# docker-entrypoint.sh). sh only changes directory; the command is then exec'd
# from its own arguments, not parsed by it.
command='env HOME=/home/lifetracker setpriv --reuid=lifetracker --regid=lifetracker --init-groups'
command+=' sh -c '"$(squote 'cd "${LIFETRACKER_APP_DIR:-/app}" && exec "$@"')"' sh'
for arg in "$@"; do
  command+=" $(squote "$arg")"
done

pty=()
if [ -t 0 ] && [ -t 1 ]; then
  pty=(--pty)
fi

exec "$(dirname "$0")/flyctl.sh" ssh console ${pty[@]+"${pty[@]}"} --command="$command"
