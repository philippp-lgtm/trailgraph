#!/usr/bin/env bash
# ci-promote-image.sh - tags the candidate image of a release commit with the release version instead of rebuilding it
# (infra DECISIONS.md I32). Promotes only when ALL hold (anything else, including every error, means "build in full"):
#   - the image inputs (INPUT_PATHS) of the tagged commit are those of a commit Y on the first-parent history of the tagged
#     commit (Y = the newest first-parent commit that changed them; the tagged commit itself when it changed them),
#   - the push run of BUILD_WORKFLOW on main for Y (exactly one) recorded the digest it pushed in the artifact ARTIFACT_NAME
#     (candidate-digest.txt: image=, commit=, digest=); that digest, not the mutable tag, is what gets promoted. The tag
#     IMAGE:sha-<Y> must still name it (cross-check), and the manifest must be a plain single-platform linux/amd64 image
#     manifest (not an index) with the label org.opencontainers.image.revision=<Y> (sanity, not proof: labels are not trusted),
#   - every workflow in CANDIDATE_WORKFLOWS has runs for push to main on Y (the commit that built the candidate), and every
#     workflow in TREE_WORKFLOWS has runs for push to main on the tagged commit itself (the checks of the tagged tree, e.g. the
#     test suite, when it differs from Y); ALL those runs are completed first-attempt successes (a green re-run hides a
#     failed attempt, as in I31/I15),
#   - IMAGE:VERSION_TAG does not exist yet, or already points at that same digest (a re-run of the job).
# The promotion PUTs the very bytes of the candidate manifest (fetched by digest and checked against it) under the version tag
# through the registry API, then reads the new tag back: a different digest is an error (exit 1), because the version tag then
# exists and must not be built over. `docker buildx imagetools create` is no option: it always wraps its source in a new index
# with a digest of its own (seen live, 2026-10-27).
# Prints the decision and writes promoted=true|false, digest=sha256:... and candidate=<Y> to $GITHUB_OUTPUT.
# Needs: git (full history), docker buildx logged in to the registry (docker/login-action), curl, sha256sum, gh and jq,
# unzip, GH_TOKEN with actions: read and packages: write (the registry token request uses it). Env: BUILD_WORKFLOW,
# ARTIFACT_NAME, IMAGE (ghcr.io/<owner>/<name>, no tag), VERSION_TAG, INPUT_PATHS (space separated
# pathspecs), CANDIDATE_WORKFLOWS and TREE_WORKFLOWS (space separated workflow file names; the latter may be empty),
# GITHUB_REPOSITORY; COMMIT (default GITHUB_SHA).
set -u

# A missing input is not an error of the release: it means "build in full" (the same as every other doubt).
image=${IMAGE:-}; version=${VERSION_TAG:-}; commit=${COMMIT:-${GITHUB_SHA:-}}; paths=${INPUT_PATHS:-}
cand_wfs=${CANDIDATE_WORKFLOWS:-}; tree_wfs=${TREE_WORKFLOWS:-}; build_wf=${BUILD_WORKFLOW:-}; artifact=${ARTIFACT_NAME:-}
repo=${GITHUB_REPOSITORY:-}; branch=${CI_PROMOTE_BRANCH:-main}
version_re=${VERSION_PATTERN:-^v[0-9]+\.[0-9]+\.[0-9]+$}   # the repository's release tag format (ERE); it ends up in registry URLs
missing=""
for v in IMAGE VERSION_TAG INPUT_PATHS CANDIDATE_WORKFLOWS BUILD_WORKFLOW ARTIFACT_NAME GITHUB_REPOSITORY GH_TOKEN; do
  [ -n "${!v:-}" ] || missing="$missing $v"
done
[ -n "$commit" ] || missing="$missing GITHUB_SHA"

digest=""; cand=""; mt=""; mf=""; bearer=""; zf=""
repo_path=${image#ghcr.io/}

# green_runs SHA WORKFLOW_FILE: every push run of the workflow on SHA on the branch is a completed first-attempt success (and there is one)
green_runs() {
  local sha=$1 wf=$2 runs ok
  runs=$(gh api "repos/$repo/actions/workflows/$wf/runs?head_sha=$sha&event=push&per_page=100" 2>/dev/null) || { echo "cannot list the runs of $wf for ${sha:0:7}"; return 1; }
  ok=$(jq -r --arg h "$sha" --arg r "$repo" --arg p ".github/workflows/$wf" --arg b "$branch" '
    if (.total_count // 0) > (.workflow_runs | length) then "too many runs" else
    [.workflow_runs[] | select(.head_sha == $h and .head_branch == $b and .event == "push" and .path == $p and .head_repository.full_name == $r)]
    | if length == 0 then "no push run on \($b)"
      elif all(.[]; .status == "completed" and .conclusion == "success" and .run_attempt == 1) then "ok"
      else "a run is not a completed first-attempt success" end
    end' <<<"$runs" 2>/dev/null) || ok="unreadable run list"
  [ "$ok" = ok ] || { echo "$wf on ${sha:0:7}: $ok"; return 1; }
}

# recorded_digest: sets $digest from the artifact $ARTIFACT_NAME of the ONE push run of $BUILD_WORKFLOW on main for $cand
# (pull_request and branch runs cannot be such a run: the head branch, the event and the head sha are matched).
recorded_digest() {
  local runs ids n id arts aid zip txt
  runs=$(gh api "repos/$repo/actions/workflows/$build_wf/runs?head_sha=$cand&event=push&per_page=100" 2>/dev/null) || { echo "cannot list the runs of $build_wf"; return 1; }
  ids=$(jq -r --arg h "$cand" --arg r "$repo" --arg p ".github/workflows/$build_wf" --arg b "$branch" \
    '.workflow_runs[] | select(.head_sha == $h and .head_branch == $b and .event == "push" and .path == $p and .head_repository.full_name == $r) | .id' <<<"$runs" 2>/dev/null) \
    || { echo "unreadable run list of $build_wf"; return 1; }
  n=$(grep -c . <<<"$ids")
  [ "$n" = 1 ] || { echo "$build_wf has $n push runs on $branch for ${cand:0:7}, expected exactly 1"; return 1; }
  id=$ids
  arts=$(gh api "repos/$repo/actions/runs/$id/artifacts?per_page=100" 2>/dev/null) || { echo "cannot list the artifacts of run $id"; return 1; }
  aid=$(jq -r --arg n "$artifact" --arg h "$cand" --arg b "$branch" --argjson id "$id" \
    '[.artifacts[] | select(.name == $n and .expired == false and .workflow_run.id == $id and .workflow_run.head_sha == $h and .workflow_run.head_branch == $b)]
     | if length == 1 then .[0].id else empty end' <<<"$arts" 2>/dev/null)
  [ -n "$aid" ] || { echo "run $id has no single unexpired artifact $artifact"; return 1; }
  zip=$(mktemp); zf="$zip"
  gh api "repos/$repo/actions/artifacts/$aid/zip" > "$zip" 2>/dev/null || { echo "cannot download artifact $aid"; return 1; }
  txt=$(unzip -p "$zip" candidate-digest.txt 2>/dev/null) || { echo "artifact $aid has no candidate-digest.txt"; return 1; }
  [ "$(grep -c '^image=' <<<"$txt")" = 1 ] && [ "$(grep -c '^commit=' <<<"$txt")" = 1 ] && [ "$(grep -c '^digest=' <<<"$txt")" = 1 ] \
    || { echo "artifact $aid is malformed"; return 1; }
  [ "$(sed -n 's/^image=//p' <<<"$txt")" = "$image" ] && [ "$(sed -n 's/^commit=//p' <<<"$txt")" = "$cand" ] \
    || { echo "artifact $aid names another image or commit"; return 1; }
  digest=$(sed -n 's/^digest=//p' <<<"$txt")
  [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || { digest=""; echo "artifact $aid holds no digest"; return 1; }
}

# decide: prints the reason on stdout, returns 0 = promote, 1 = build in full
decide() {
  [ -z "$missing" ] || { echo "missing input:$missing"; return 1; }
  [[ $version =~ $version_re ]] || { echo "version tag '$version' does not match $version_re"; return 1; }
  [[ $image =~ ^ghcr\.io/[a-z0-9._-]+/[a-z0-9._/-]+$ ]] || { echo "unexpected image name '$image'"; return 1; }
  [[ $commit =~ ^[0-9a-f]{40}$ ]] || { echo "bad commit $commit"; return 1; }
  set -f
  # shellcheck disable=SC2086
  cand=$(git rev-list --first-parent -1 "$commit" -- $paths 2>/dev/null) || { set +f; echo "cannot read the history of ${commit:0:7}"; return 1; }
  set +f
  [[ $cand =~ ^[0-9a-f]{40}$ ]] || { echo "no commit changed the image inputs ($paths)"; return 1; }

  # 1. The runs that built and tested the candidate (and the tagged tree) are green first attempts.
  local wf
  for wf in $build_wf $cand_wfs; do green_runs "$cand" "$wf" || return 1; done
  for wf in $tree_wfs; do green_runs "$commit" "$wf" || return 1; done

  # 2. The digest is the one that push run on main RECORDED (artifact of that very run, whose head is the candidate commit on
  #    main). The mutable tag sha-<Y> is only a cross-check: whoever can push packages can move a tag and forge labels, but
  #    cannot add an artifact to a run of main.
  recorded_digest || return 1

  local ref="$image:sha-$cand" out
  out=$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}' 2>&1) \
    || { echo "no candidate tag $ref (${out##*: })"; return 1; }
  [ "$out" = "$digest" ] || { echo "tag $ref names $out, the build run recorded $digest: not promoting"; return 1; }
  out=$(docker buildx imagetools inspect "$image@$digest" --format '{{.Manifest.MediaType}}' 2>&1) || { echo "cannot read the recorded manifest $digest"; return 1; }
  mt=$out
  case $mt in
    application/vnd.oci.image.manifest.v1+json|application/vnd.docker.distribution.manifest.v2+json) ;;
    *) echo "candidate $digest is not a plain image manifest ($mt)"; return 1 ;;
  esac

  local cfg got
  cfg=$(docker buildx imagetools inspect "$image@$digest" --format '{{json .Image}}' 2>/dev/null) || { echo "cannot read the image config of $digest"; return 1; }
  got=$(jq -r '[.os, .architecture, .config.Labels["org.opencontainers.image.revision"]] | join(" ")' <<<"$cfg" 2>/dev/null) || got=""
  [ "$got" = "linux amd64 $cand" ] || { echo "candidate $digest is not a linux/amd64 image labelled with revision ${cand:0:7} (found: ${got:-unreadable})"; return 1; }

  local have
  if have=$(docker buildx imagetools inspect "$image:$version" --format '{{.Manifest.Digest}}' 2>&1); then
    [ "$have" = "$digest" ] || { echo "$image:$version exists with another digest ($have)"; return 1; }
  else
    shopt -s nocasematch
    [[ $have == *"not found"* || $have == *"404"* ]] || { shopt -u nocasematch; echo "cannot tell whether $image:$version exists (${have##*: })"; return 1; }
    shopt -u nocasematch
  fi
  # The candidate manifest, byte for byte, and a registry token that may push: both before anything is written.
  mf=$(mktemp)
  # Credentials go to curl on stdin (-K -), never into its argument list (visible in /proc to every process of the runner).
  bearer=$(printf 'user = "x-access-token:%s"\n' "$GH_TOKEN" | curl -fsS -K - "https://ghcr.io/token?service=ghcr.io&scope=repository:$repo_path:pull,push" 2>/dev/null | jq -er .token 2>/dev/null) \
    || { echo "no registry token with push access to $repo_path"; return 1; }
  printf 'header = "Authorization: Bearer %s"\n' "$bearer" | curl -fsS -K - -H "Accept: $mt" -o "$mf" "https://ghcr.io/v2/$repo_path/manifests/$digest" 2>/dev/null \
    || { echo "cannot fetch the manifest $digest"; return 1; }
  [ "sha256:$(sha256sum "$mf" | cut -d' ' -f1)" = "$digest" ] || { echo "the fetched manifest does not hash to $digest"; return 1; }
  echo "candidate sha-${cand:0:7} ($digest), built by green push runs of: $cand_wfs${tree_wfs:+; tagged tree checked by: $tree_wfs}"
  return 0
}

# Not in a command substitution: digest and cand must survive the call.
rf=$(mktemp); trap 'rm -f "$rf" "$mf" "$zf"' EXIT
if decide >"$rf"; then promote=true; else promote=false; fi
reason=$(<"$rf")
if [ "$promote" = true ]; then
  if ! printf 'header = "Authorization: Bearer %s"\n' "$bearer" | curl -fsS -K - -X PUT -H "Content-Type: $mt" --data-binary "@$mf" -o /dev/null \
       "https://ghcr.io/v2/$repo_path/manifests/$version"; then
    # The tag may or may not exist now; do not fall back to a build that would write over it.
    echo "::error::tagging $image:$version failed; check the registry before retrying" >&2; exit 1
  fi
  now=$(docker buildx imagetools inspect "$image:$version" --format '{{.Manifest.Digest}}' 2>/dev/null) || now=""
  [ "$now" = "$digest" ] || { echo "::error::$image:$version is $now after the promotion, expected $digest" >&2; exit 1; }
  echo "promoted: $reason -> $image:$version@$digest (no rebuild)"
else
  digest=""; echo "full build: $reason"
fi
[ -z "${GITHUB_OUTPUT:-}" ] || { echo "promoted=$promote"; echo "digest=$digest"; echo "candidate=$cand"; } >> "$GITHUB_OUTPUT"
[ -z "${GITHUB_STEP_SUMMARY:-}" ] || echo "ci-promote-image: promoted=$promote - $reason" >> "$GITHUB_STEP_SUMMARY"
exit 0
