#!/usr/bin/env bash
# Build the example graph "company" in a throwaway installation outside the checkout.
#
#   BASE=<new or empty directory> examples/company/setup.sh [1.0.0|1.1.0]
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
if [ -e "$BASE" ] && [ -n "$(ls -A "$BASE" 2>/dev/null)" ]; then
  echo "$BASE is not empty; use a new or empty directory" >&2
  exit 1
fi
mkdir -p "$BASE"
BASE="$(cd "$BASE" && pwd)"
mkdir -p "$BASE/home/.claude" "$BASE/data" "$BASE/config" "$BASE/cache" "$BASE/work"
state="$(mktemp -d /tmp/gv.XXXXXX)"
trap 'echo "setup failed; remove what it created with: rm -rf $(printf %q "$BASE") $(printf %q "$state")" >&2' ERR

# The installation must not see the settings, tokens or skill directory of the caller.
{
  # shellcheck disable=SC2016
  echo 'unset CLAUDE_CONFIG_DIR $(env | grep -o "^GRAPH_CLI_[A-Z_]*")'
  printf 'export HOME=%q\n' "$BASE/home"
  printf 'export XDG_DATA_HOME=%q\n' "$BASE/data"
  printf 'export XDG_CONFIG_HOME=%q\n' "$BASE/config"
  printf 'export XDG_CACHE_HOME=%q\n' "$BASE/cache"
  printf 'export XDG_STATE_HOME=%q\n' "$state"
  echo 'export GRAPH_CLI_GRAPH=company'
  printf 'cd %q\n' "$BASE/work"
} >"$BASE/env.sh"
# shellcheck disable=SC1091
source "$BASE/env.sh"

graph-admin init
graph skills install --scope user
"$here/load.sh" "$@"

echo
echo "The example graph is ready. In a shell:  source $(printf %q "$BASE/env.sh") && graph-admin view"
echo "Remove it with:  rm -rf $(printf %q "$BASE") $(printf %q "$state")"
