#!/usr/bin/env bash
# Print the ID of the production Fly Machine, via ./flyctl.sh (so FLY_APP comes
# from .env), e.g. for `./flyctl.sh machine status $(./fly-machine-id.sh)`. The
# app runs only one Machine (see README.md's Fly.io section), so this prints the
# first one listed. Needs jq. Fails if flyctl fails or the app has no Machine.
set -euo pipefail

# With no Machine, .[0].id is null: fail with a message rather than print "null".
"$(dirname "$0")/flyctl.sh" machine list --json | jq -r '.[0].id // error("the app has no Fly Machine")'
