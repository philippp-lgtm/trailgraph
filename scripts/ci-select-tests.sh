#!/usr/bin/env bash
# ci-select-tests.sh - decides which tests a pull request run executes: the whole suite or the selection of
# scripts/select_tests.py. Only a pull request run may select; everything else (a push to main, a tag, any other
# event) runs the whole suite, so the gate before a release stays the full suite. The answer is "full" unless ALL hold
# (anything else, including every error, means "full"):
#   - the event is a pull request, and the pull request does not carry the label ci:full (read from the API when
#     the job runs, not from the event, so a re-run sees a label added later),
#   - the base commit of the pull request (BASE_SHA) is in the checkout and holds scripts/select_tests.py: the
#     selector that RUNS is the base branch's, never the pull request's own, so a change to the selector cannot judge
#     itself. (This script, the workflow and the Dockerfile do come from the pull request: a change to them controls
#     its own run, and ci-reuse-pr-run.sh makes main run such a change itself.)
#   - the selector answers with test paths (not `full`), every one of the form tests/<name>.py without anything a shell
#     or pytest would read as more than a path.
# The change is `git diff BASE_SHA HEAD` (HEAD is the merge of the pull request into its base). The selector runs in the
# image of the lint job with no network and the checkout read-only; CI_SELECT_PYTHON (a command, for the offline test)
# replaces that.
# Writes mode=full|selected and args=<paths, space separated> to $GITHUB_OUTPUT and the reasons to the job summary.
# Needs: git with the history of the base, gh, docker (or CI_SELECT_PYTHON); GH_TOKEN with pull-requests: read;
# GITHUB_EVENT_NAME, GITHUB_REPOSITORY and, for a pull request, BASE_SHA and PR_NUMBER.
set -u

event=${GITHUB_EVENT_NAME:?}; repo=${GITHUB_REPOSITORY:?}; base=${BASE_SHA:-}; pr=${PR_NUMBER:-}
image=${CI_SELECT_IMAGE:-ghcr.io/astral-sh/uv:0.13-python3.13-trixie-slim}
tmp=$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/select.XXXXXX") && chmod 755 "$tmp" || { tmp=""; }
trap '[ -z "$tmp" ] || rm -rf "$tmp"' EXIT
args=""; detail=""

# decide: sets $reason (and $args, $detail), returns 0 = selected tests, 1 = the full suite
decide() {
  [ "$event" = pull_request ] || { reason="not a pull request ($event): the whole suite runs"; return 1; }
  [ -n "$tmp" ] || { reason="no scratch directory"; return 1; }
  [[ $base =~ ^[0-9a-f]{40}$ ]] || { reason="no base commit"; return 1; }
  [[ $pr =~ ^[0-9]+$ ]] || { reason="no pull request number"; return 1; }

  local labels
  labels=$(gh api "repos/$repo/pulls/$pr" --jq '.labels[].name' 2>/dev/null) || { reason="cannot read the labels of #$pr"; return 1; }
  ! grep -qxF 'ci:full' <<<"$labels" || { reason="label ci:full on #$pr"; return 1; }

  git cat-file -e "$base^{commit}" 2>/dev/null || { reason="base commit ${base:0:7} is not in the checkout"; return 1; }
  git show "$base:scripts/select_tests.py" > "$tmp/select_tests.py" 2>/dev/null \
    || { reason="the base ${base:0:7} has no scripts/select_tests.py yet"; return 1; }
  git diff --name-only --no-renames -z "$base" HEAD > "$tmp/changed" 2>/dev/null || { reason="cannot list the changed files"; return 1; }
  [ -s "$tmp/changed" ] || { reason="no changed files"; return 1; }

  local rc=0
  if [ -n "${CI_SELECT_PYTHON:-}" ]; then
    # shellcheck disable=SC2086
    $CI_SELECT_PYTHON "$tmp/select_tests.py" --changed-files "$tmp/changed" --root "$PWD" --explain > "$tmp/out" 2> "$tmp/err" || rc=$?
  else
    docker run --rm --network none --user "$(id -u):$(id -g)" -v "$PWD:/src:ro" -v "$tmp:/select:ro" -w /src "$image" \
      python -I /select/select_tests.py --changed-files /select/changed --root /src --explain > "$tmp/out" 2> "$tmp/err" || rc=$?
  fi
  detail=$(cat "$tmp/err" 2>/dev/null)
  [ "$rc" = 0 ] || { reason="the selector failed (exit $rc)"; return 1; }
  [ -s "$tmp/out" ] || { reason="the selector printed nothing"; return 1; }
  [ "$(head -n 1 "$tmp/out")" != full ] || { reason="the selector says full: $(sed -n 's/^select_tests: full: //p' "$tmp/err" | head -n 1)"; return 1; }
  grep -qvE '^tests/[A-Za-z0-9_./-]+\.py$' "$tmp/out" && { reason="the selector printed something that is not a test path"; return 1; }
  args=$(tr '\n' ' ' < "$tmp/out"); args=${args% }
  reason="$(grep -c . "$tmp/out") test files selected"
  return 0
}


reason=""
if decide; then mode=selected; else mode=full; args=""; fi
echo "test selection: $mode - $reason"
[ -z "$detail" ] || sed 's/^/  /' <<<"$detail"
[ -z "${GITHUB_OUTPUT:-}" ] || { echo "mode=$mode"; echo "args=$args"; } >> "$GITHUB_OUTPUT"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### Test selection: $mode"; echo; echo "$reason"
    if [ -n "$detail" ]; then echo; echo '<details><summary>Selector output</summary>'; echo; echo '```'; echo "$detail"; echo '```'; echo; echo '</details>'; fi
  } >> "$GITHUB_STEP_SUMMARY"
fi
exit 0
