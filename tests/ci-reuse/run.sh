#!/usr/bin/env bash
# Offline test of scripts/ci-reuse-pr-run.sh (infra DECISIONS.md I31): a stub `gh` serves canned API answers, and every way the
# decision can go wrong must end in "full run". Usage: tests/ci-reuse/run.sh   (needs jq)
set -u
here=$(cd "$(dirname "$0")" && pwd)
script="$here/../../scripts/ci-reuse-pr-run.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
failed=0

M=$(printf 'a%.0s' {1..40}); P=$(printf 'b%.0s' {1..40}); H=$(printf 'c%.0s' {1..40})
T1=$(printf '1%.0s' {1..40}); T2=$(printf '2%.0s' {1..40})
R=owner/repo

# The stub answers `gh api repos/owner/repo/<path>` from $FX/<path with / and ? replaced by _>; no file = API error.
mkdir -p "$tmp/bin"
cat > "$tmp/bin/gh" <<'STUB'
#!/usr/bin/env bash
[ "$1" = api ] || exit 2
f="$FX/$(sed -e 's|^repos/owner/repo/||' -e 's|[/?&=]|_|g' <<<"$2")"
[ -f "$f" ] && cat "$f" || exit 1
STUB
chmod +x "$tmp/bin/gh"

# the stub turns / ? & = of the request path into _
runs_file() { echo "$1/actions_workflows_validate.yml_runs_head_sha_${H}_event_pull_request_per_page_100"; }

# fixture: the happy path, one merged PR #7 whose head was rebased on the base side and has a green first-attempt run
fixture() {
  FX="$tmp/fx$1"; rm -rf "$FX"; mkdir -p "$FX"
  echo "[{\"number\":7,\"merged_at\":\"2026-10-27T00:00:00Z\",\"merge_commit_sha\":\"$M\",\"base\":{\"ref\":\"main\"},\"head\":{\"sha\":\"$H\"}}]" > "$FX/commits_${M}_pulls_per_page_100"
  echo "[{\"number\":7}]" > "$FX/commits_${H}_pulls_per_page_100"
  echo "{\"tree\":{\"sha\":\"$T1\"},\"parents\":[{\"sha\":\"$P\"},{\"sha\":\"$H\"}]}" > "$FX/git_commits_$M"
  echo "{\"tree\":{\"sha\":\"$T1\"},\"parents\":[{\"sha\":\"$P\"}]}" > "$FX/git_commits_$H"
  echo '{"status":"ahead"}' > "$FX/compare_${P}...${H}_per_page_1"
  echo "{\"workflow_runs\":[{\"id\":99,\"head_sha\":\"$H\",\"head_repository\":{\"full_name\":\"$R\"},\"created_at\":\"2026-10-27T01:00:00Z\",\"pull_requests\":[{\"number\":7,\"base\":{\"ref\":\"main\",\"repo\":{\"url\":\"https://api.github.com/repos/$R\"}}}],\"path\":\".github/workflows/validate.yml\",\"status\":\"completed\",\"conclusion\":\"success\",\"run_attempt\":1}]}" > "$(runs_file "$FX")"
  echo "$FX"
}

# expect NAME reuse|full PATTERN [EVENT REF]  (fixture dir in $FX, may be edited before the call)
expect() {
  local name=$1 want=$2 pat=$3 ev=${4:-push} ref=${5:-refs/heads/main} out
  out=$(PATH="$tmp/bin:$PATH" FX="$FX" GITHUB_REPOSITORY=$R GITHUB_SHA=$M GITHUB_EVENT_NAME=$ev GITHUB_REF=$ref \
        GITHUB_OUTPUT="$tmp/out" "$script" validate.yml 2>&1); local rc=$?
  local got; got=$(tail -1 "$tmp/out" 2>/dev/null)
  if [ "$rc" -ne 0 ] || [ "$got" != "reuse=$([ "$want" = reuse ] && echo true || echo false)" ] || ! grep -Eq "$pat" <<<"$out"; then
    echo "FAIL $name: want $want /$pat/, got rc=$rc $got"; sed 's/^/       /' <<<"$out"; failed=1
  else echo "ok   $name -> $want"; fi
}

good() { fixture "$1" >/dev/null; }

good 1; expect "identical tree, green PR run" reuse 'tree identical to PR #7 head ccccccc, validated in run 99; not re-run'
good 2; expect "tag push" full 'not a push to main' push refs/tags/v1.0.0
good 3; expect "pull request event" full 'not a push to main' pull_request refs/pull/7/merge
good 4; expect "schedule" full 'not a push to main' schedule refs/heads/main
good 5; echo "[]" > "$FX/commits_${M}_pulls_per_page_100"; expect "direct push, no PR" full 'not the merge commit of exactly one'
good 6; jq '.[0].merged_at=null' "$FX/commits_${M}_pulls_per_page_100" > "$FX/x" && mv "$FX/x" "$FX/commits_${M}_pulls_per_page_100"; expect "PR not merged" full 'not the merge commit'
good 7; jq '.[0].merge_commit_sha="dead"' "$FX/commits_${M}_pulls_per_page_100" > "$FX/x" && mv "$FX/x" "$FX/commits_${M}_pulls_per_page_100"; expect "PR merged as another commit" full 'not the merge commit'
good 8; jq '.[0].base.ref="release"' "$FX/commits_${M}_pulls_per_page_100" > "$FX/x" && mv "$FX/x" "$FX/commits_${M}_pulls_per_page_100"; expect "PR into another base" full 'not the merge commit'
good 9; jq --arg t "$T2" '.tree.sha=$t' "$FX/git_commits_$H" > "$FX/x" && mv "$FX/x" "$FX/git_commits_$H"; expect "tree differs from PR head" full 'tree of aaaaaaa differs'
good 10; echo '{"status":"diverged"}' > "$FX/compare_${P}...${H}_per_page_1"; expect "head not rebased on the base side" full 'not rebased on'
good 11; echo '{"status":"identical"}' > "$FX/compare_${P}...${H}_per_page_1"; expect "head equals the base side" reuse 'tree identical'
good 12; jq '.workflow_runs[0].conclusion="failure"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "PR run failed" full 'no usable green'
good 13; jq '.workflow_runs[0].run_attempt=2' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "PR run green only on attempt 2" full 'no usable green'
good 14; jq '.workflow_runs[0].head_sha="dead"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "run of another head SHA" full 'no usable green'
good 15; jq '.workflow_runs[0].path=".github/workflows/other.yml"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "run of another workflow" full 'no usable green'
good 16; jq '.workflow_runs[0].head_repository.full_name="fork/repo"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "run from a fork" full 'no usable green'
good 17; jq '.workflow_runs[0].status="in_progress"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "PR run still running" full 'no usable green'
good 18; rm "$FX/commits_${M}_pulls_per_page_100"; expect "API error listing pull requests" full 'cannot list'
good 19; rm "$FX/git_commits_$H"; expect "API error reading the head commit" full 'cannot read the commits'
good 20; rm "$FX/compare_${P}...${H}_per_page_1"; expect "API error on compare" full 'not rebased on .*unknown'
good 21; rm "$(runs_file "$FX")"; expect "API error listing runs" full 'cannot list the runs'
good 22; echo 'not json' > "$FX/commits_${M}_pulls_per_page_100"; expect "garbage answer" full 'unreadable|cannot|not the merge commit'
good 23; jq '.workflow_runs[0].pull_requests[0].number=8' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "green run belongs to another PR" full 'no usable green'
good 24; jq '.workflow_runs[0].pull_requests[0].base.ref="release"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "green run belongs to another base" full 'no usable green'
good 25; jq '.workflow_runs[0].pull_requests[0].base.repo.url="https://api.github.com/repos/fork/repo"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "green run belongs to another base repository" full 'no usable green'
good 26; jq '.workflow_runs[0].pull_requests=[]' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "run names no pull request (branch deleted), head is unique to #7" reuse 'run 99'
good 27; jq '.workflow_runs += [(.workflow_runs[0] | .id=98 | .conclusion="failure" | .created_at="2026-10-27T00:30:00Z")]' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "failed run, later success, same head" full 'no usable green'
good 28; jq '.workflow_runs += [(.workflow_runs[0] | .id=98 | .conclusion="cancelled" | .created_at="2026-10-27T00:30:00Z")]' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "cancelled run, later success, same head" full 'no usable green'
good 29; jq '.workflow_runs += [(.workflow_runs[0] | .id=100 | .status="in_progress" | .conclusion=null | .created_at="2026-10-27T02:00:00Z")]' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "newest run not finished" full 'no usable green'
good 30; jq '.workflow_runs += [(.workflow_runs[0] | .id=97 | .run_attempt=2 | .created_at="2026-10-27T00:30:00Z")]' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "older run green only on attempt 2, newer first-attempt success" full 'no usable green'

good 31; echo '[{"number":7},{"number":9}]' > "$FX/commits_${H}_pulls_per_page_100"; jq '.workflow_runs[0].pull_requests=[]' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "head also belongs to another PR (e.g. into another base)" full 'belongs to other pull requests'
good 32; rm "$FX/commits_${H}_pulls_per_page_100"; expect "API error listing the PRs of the head" full 'cannot list the pull requests of head'

good 33; jq '.workflow_runs[0].conclusion="neutral"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "conclusion neutral" full 'no usable green'
good 34; jq '.workflow_runs[0].conclusion="startup_failure"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "conclusion startup_failure" full 'no usable green'
good 35; jq '.workflow_runs[0].conclusion="skipped"' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "conclusion skipped" full 'no usable green'
good 36; jq '.total_count=101' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "more runs than one page" full 'no usable green'
good 37; jq '.workflow_runs += [(.workflow_runs[0] | .id=96 | .created_at="2026-10-27T00:10:00Z")] | .total_count=2' "$(runs_file "$FX")" > "$FX/x" && mv "$FX/x" "$(runs_file "$FX")"; expect "two first-attempt successes" reuse 'run 99'

good 38; jq -n --arg m "$M" '[range(0;100) | {number:., merged_at:null, merge_commit_sha:"x", base:{ref:"main"}, head:{sha:"y"}}] | .[0] = {number:7, merged_at:"2026-10-27T00:00:00Z", merge_commit_sha:$m, base:{ref:"main"}, head:{sha:"'$H'"}}' > "$FX/commits_${M}_pulls_per_page_100"; expect "pull request list of the commit hits the page size" full 'possibly truncated'
good 39; jq -n '[range(0;100) | {number:.}]' > "$FX/commits_${H}_pulls_per_page_100"; expect "pull request list of the head hits the page size" full 'possibly truncated'

exit "$failed"
