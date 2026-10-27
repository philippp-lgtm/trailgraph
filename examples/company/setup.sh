#!/usr/bin/env bash
# Build the example graph "company" in a throwaway installation outside the checkout.
#
#   BASE=<empty or new directory> examples/company/setup.sh [1.0.0|1.1.0]
#
# Needs `graph` and `graph-admin` on PATH (from a checkout: `uv sync`, then
# PATH="$PWD/.venv/bin:$PATH"). Everything lives below $BASE: data root, config, a home
# directory and a working directory. Only the state directory (viewer sockets) is a short
# directory in /tmp, because a Unix socket path is limited to about 100 characters.
# Afterwards `source "$BASE/env.sh"` selects the installation in a shell; remove it with
# `rm -rf "$BASE"` and the state directory named in env.sh.
set -euo pipefail

: "${BASE:?set BASE to the directory the throwaway installation is created in}"
case "$BASE" in /*) ;; *)
  echo "BASE must be an absolute path" >&2
  exit 2
  ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$BASE"
BASE="$(cd "$BASE" && pwd)"
if [ -e "$BASE/env.sh" ]; then
  echo "$BASE holds an installation already; use a new directory" >&2
  exit 1
fi
mkdir -p "$BASE/home/.claude" "$BASE/data" "$BASE/config" "$BASE/cache" "$BASE/work"
state="$(mktemp -d /tmp/gv.XXXXXX)"

# The installation must not see the settings, tokens or skill directory of the caller.
cat >"$BASE/env.sh" <<EOF
unset CLAUDE_CONFIG_DIR \$(env | grep -o '^GRAPH_CLI_[A-Z_]*')
export HOME='$BASE/home'
export XDG_DATA_HOME='$BASE/data'
export XDG_CONFIG_HOME='$BASE/config'
export XDG_CACHE_HOME='$BASE/cache'
export XDG_STATE_HOME='$state'
export GRAPH_CLI_GRAPH=company
cd '$BASE/work'
EOF
# shellcheck disable=SC1091
source "$BASE/env.sh"

graph-admin init
graph skills install --scope user
"$here/load.sh" "$@"

echo
echo "The example graph is ready. In a shell:  source '$BASE/env.sh' && graph-admin view"
echo "Remove it with:  rm -rf '$BASE' '$state'"
