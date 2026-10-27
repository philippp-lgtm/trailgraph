#!/usr/bin/env bash
# ci-reuse-pr-run.sh WORKFLOW_FILE - decides whether a push to main needs its own check run (infra DECISIONS.md I31).
# Reuse only when ALL hold (anything else, including every API error, means "run the full check"):
#   - the event is a push to the default branch (tags, pull requests, schedules and the like always run in full),
#   - the pushed commit is the merge commit of exactly one merged pull request,
#   - the base side of that merge (first parent) is an ancestor of the pull request head, i.e. the head was
#     rebased on main right before the merge (I30), so the pull request run tested the head tree itself and not
#     a test merge with an older base,
#   - the commit's tree equals the tree of the pull request head: identical inputs,
#   - the newest `pull_request` run of the same workflow file on that exact head, belonging to this pull request
#     (into this repository's main; the head belongs to no other pull request); ALL its runs are completed, successful
#     first attempts (a green re-run hides a failed attempt and must stay visible to the tag check, I15).
# Prints the decision and writes `reuse=true|false` to $GITHUB_OUTPUT; the steps after it run `if: steps.<id>.outputs.reuse != 'true'`.
# Needs: gh and jq; GH_TOKEN with actions: read and pull-requests: read; GITHUB_REPOSITORY, GITHUB_SHA,
# GITHUB_EVENT_NAME, GITHUB_REF (set by Actions). Only the GitHub API is read, no checkout is needed.
set -u

wf=${1:?workflow file name, e.g. validate.yml}
repo=${GITHUB_REPOSITORY:?}; sha=${GITHUB_SHA:?}; event=${GITHUB_EVENT_NAME:?}; ref=${GITHUB_REF:?}
branch=${CI_REUSE_BRANCH:-main}

api() { gh api "repos/$repo/$1" 2>/dev/null; }

# decide: prints the reason on stdout, returns 0 = reuse, 1 = run in full
decide() {
  [ "$event" = push ] && [ "$ref" = "refs/heads/$branch" ] || { echo "not a push to $branch ($event $ref)"; return 1; }

  local pulls pr n head
  pulls=$(api "commits/$sha/pulls?per_page=100") || { echo "cannot list the pull requests of ${sha:0:7}"; return 1; }
  pr=$(jq -c --arg sha "$sha" --arg b "$branch" \
    '[.[] | select(.merged_at != null and .merge_commit_sha == $sha and .base.ref == $b)]' <<<"$pulls") \
    || { echo "unreadable pull request list"; return 1; }
  [ "$(jq length <<<"$pr")" = 1 ] || { echo "${sha:0:7} is not the merge commit of exactly one pull request"; return 1; }
  n=$(jq -r '.[0].number' <<<"$pr"); head=$(jq -r '.[0].head.sha' <<<"$pr")
  [[ $head =~ ^[0-9a-f]{40}$ ]] || { echo "pull request #$n has no head SHA"; return 1; }

  local mc hc tree_m tree_h parent
  mc=$(api "git/commits/$sha") && hc=$(api "git/commits/$head") || { echo "cannot read the commits ${sha:0:7} / ${head:0:7}"; return 1; }
  tree_m=$(jq -r '.tree.sha' <<<"$mc"); tree_h=$(jq -r '.tree.sha' <<<"$hc"); parent=$(jq -r '.parents[0].sha // empty' <<<"$mc")
  [[ $tree_m =~ ^[0-9a-f]{40}$ && $tree_h =~ ^[0-9a-f]{40}$ && -n $parent ]] || { echo "unreadable commit objects"; return 1; }
  [ "$tree_m" = "$tree_h" ] || { echo "tree of ${sha:0:7} differs from head ${head:0:7} of #$n"; return 1; }

  local st
  st=$(api "compare/$parent...$head?per_page=1" | jq -r '.status // empty') || st=""
  case $st in identical|ahead) ;; *) echo "head ${head:0:7} of #$n is not rebased on ${parent:0:7} (compare: ${st:-unknown})"; return 1 ;; esac

  # GitHub empties a run's pull_requests once the head branch is deleted, so the run cannot always name its pull
  # request. Then the head commit itself must belong to this pull request only: a pull_request run of that head can
  # then come from no other pull request (e.g. one into another base, which would have tested another tree).
  local hp
  hp=$(api "commits/$head/pulls?per_page=100") || { echo "cannot list the pull requests of head ${head:0:7}"; return 1; }
  [ "$(jq -c --argjson n "$n" '[.[].number] == [$n]' <<<"$hp" 2>/dev/null)" = true ] \
    || { echo "head ${head:0:7} belongs to other pull requests than #$n as well"; return 1; }

  local runs run
  runs=$(api "actions/workflows/$wf/runs?head_sha=$head&event=pull_request&per_page=100") || { echo "cannot list the runs of $wf for ${head:0:7}"; return 1; }
  # The runs of this workflow on this exact head. One that names pull requests must name #n into $branch of this
  # repository. Allowlist: ALL of them must be completed, conclusion success and a first attempt (a failed, cancelled,
  # neutral, skipped, stale, startup_failure run, or a run re-run to green, is never erased by a later green one).
  # More runs than one page holds: full run.
  run=$(jq -r --arg h "$head" --arg r "$repo" --arg p ".github/workflows/$wf" --argjson n "$n" --arg b "$branch" '
    if (.total_count // 0) > (.workflow_runs | length) then empty else
    [.workflow_runs[]
     | select(.head_sha == $h and .head_repository.full_name == $r and .path == $p
              and ((.pull_requests // []) | length == 0
                   or any(.[]; .number == $n and .base.ref == $b and (.base.repo.url | endswith("/repos/" + $r)))))]
    | sort_by(.created_at) | reverse
    | if length > 0 and all(.[]; .status == "completed" and .conclusion == "success" and .run_attempt == 1) then .[0].id else empty end
    end // empty' <<<"$runs") \
    || { echo "unreadable run list"; return 1; }
  [ -n "$run" ] || { echo "no usable green first-attempt pull_request run of $wf for head ${head:0:7} of #$n (none, or one of them not a first-attempt success, or too many runs)"; return 1; }

  echo "tree identical to PR #$n head ${head:0:7}, validated in run $run; not re-run"
  return 0
}

if reason=$(decide); then reuse=true; else reuse=false; fi
if [ "$reuse" = true ]; then echo "reuse: $reason"; else echo "full run: $reason"; fi
[ -z "${GITHUB_OUTPUT:-}" ] || echo "reuse=$reuse" >> "$GITHUB_OUTPUT"
[ -z "${GITHUB_STEP_SUMMARY:-}" ] || echo "ci-reuse-pr-run: $reuse - $reason" >> "$GITHUB_STEP_SUMMARY"
exit 0
