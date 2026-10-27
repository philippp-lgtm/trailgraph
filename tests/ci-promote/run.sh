#!/usr/bin/env bash
# Offline test of scripts/ci-promote-image.sh (infra DECISIONS.md I32): a real throw-away git repository, a stub `docker`
# (registry reads), a stub `curl` (registry token, manifest fetch and PUT) and a stub `gh` (the runs API). Every way the decision can go wrong must end in "full build", and a
# promotion must add the tag to the candidate's digest. Usage: tests/ci-promote/run.sh   (needs git and jq)
set -u
here=$(cd "$(dirname "$0")" && pwd)
script="$here/../../scripts/ci-promote-image.sh"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
failed=0
R=owner/repo; IMG=ghcr.io/owner/app
MANIFEST='{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","note":"the candidate"}'
D1=sha256:$(printf '%s' "$MANIFEST" | sha256sum | cut -d' ' -f1); D2=sha256:$(printf '2%.0s' {1..64})

# --- history: C1 (inputs + docs), C2 (docs only), then a side branch merged with --no-ff (M, first parent = C2)
repo="$tmp/repo"; git init -q "$repo"; cd "$repo" || exit 1
git config user.email t@t; git config user.name t; git config commit.gpgsign false
mkdir img docs; echo a > img/f; echo a > docs/d; git add -A; git commit -qm C1; C1=$(git rev-parse HEAD)
echo b > docs/d; git commit -qam C2; C2=$(git rev-parse HEAD)
git checkout -qb side; echo c > img/f; git commit -qam S1; S1=$(git rev-parse HEAD)
git checkout -q -; git merge -q --no-ff side -m M; M=$(git rev-parse HEAD)
echo e > docs/d; git commit -qam C3; C3=$(git rev-parse HEAD)

mkdir -p "$tmp/bin"
# docker stub: the registry is the directory $FX. tag_<name> holds a digest, cfg_<digest> the image config, mt_<digest> the media type
cat > "$tmp/bin/docker" <<'STUB'
#!/usr/bin/env bash
[ "$1 $2" = "buildx imagetools" ] || exit 2
key() { sed -e 's|^ghcr.io/owner/app||' -e 's|[:@/]|_|g' <<<"$1"; }
case $3 in
  inspect)
    ref=$4; fmt=$6
    if [[ $ref == *@sha256:* ]]; then d=${ref#*@}; else
      [ -f "$FX/tag$(key "$ref")" ] || { echo "ERROR: $ref: ${NOTFOUND_MSG:-not found}" >&2; exit 1; }
      [ -f "$FX/neterr" ] && { echo "ERROR: $ref: dial tcp: i/o timeout" >&2; exit 1; }
      d=$(cat "$FX/tag$(key "$ref")")
    fi
    case $fmt in
      *MediaType*) echo -n "$(cat "$FX/mt_$d" 2>/dev/null || echo application/vnd.oci.image.manifest.v1+json)" ;;
      *Manifest.Digest*) echo -n "$d" ;;
      *json*) [ -f "$FX/cfg_$d" ] && cat "$FX/cfg_$d" || exit 1 ;;
    esac ;;
esac
STUB
# curl stub: the registry API. The token is "tok" (no file token_fail), GET manifests/<digest> serves $FX/manifest (the bytes of D1),
# PUT manifests/<tag> stores the digest of the body as the tag (create_fail: HTTP error; create_other: another digest).
cat > "$tmp/bin/curl" <<'STUB'
#!/usr/bin/env bash
out=""; method=GET; data=""; url=""
[ -z "${CURL_ARGV:-}" ] || echo "$*" >> "$CURL_ARGV"
while [ $# -gt 0 ]; do
  case $1 in
    -o) out=$2; shift ;; -X) method=$2; shift ;; --data-binary) data=${2#@}; shift ;; -u|-H) shift ;; -K) shift; cat >/dev/null ;;
    http*) url=$1 ;;
  esac
  shift
done
write() { if [ -z "$out" ]; then cat; elif [ "$out" = /dev/null ]; then cat >/dev/null; else cat > "$out"; fi; }
case $url in
  *ghcr.io/token*) [ -f "$FX/token_fail" ] && exit 22; echo '{"token":"tok"}' | write ;;
  */manifests/sha256:*) cat "$FX/manifest" | write ;;
  */manifests/*) [ "$method" = PUT ] || exit 22
    [ -f "$FX/create_fail" ] && exit 22
    t=${url##*/}; echo "$t $(sha256sum < "$data" | cut -d' ' -f1)" >> "$FX/created"
    if [ -f "$FX/create_other" ]; then cat "$FX/create_other" > "$FX/tag_$t"; else echo -n "sha256:$(sha256sum < "$data" | cut -d' ' -f1)" > "$FX/tag_$t"; fi ;;
esac
STUB
# gh stub: `gh api repos/owner/repo/<path>`: workflow runs from $FX/runs_<wf>, run artifacts from $FX/artifacts, the zip from $FX/zip
cat > "$tmp/bin/gh" <<'STUB'
#!/usr/bin/env bash
[ "$1" = api ] || exit 2
p=${2#repos/owner/repo/}
case $p in
  actions/workflows/*/runs\?*) wf=${p#actions/workflows/}; wf=${wf%%/runs*}; f="$FX/runs_$wf" ;;
  actions/runs/*/artifacts\?*) f="$FX/artifacts" ;;
  actions/artifacts/*/zip) f="$FX/zip" ;;
  *) exit 1 ;;
esac
[ -f "$f" ] && cat "$f" || exit 1
STUB
chmod +x "$tmp/bin/docker" "$tmp/bin/gh" "$tmp/bin/curl"

run_json() { # sha [conclusion] [attempt] [branch] [path-wf]
  echo "{\"total_count\":1,\"workflow_runs\":[{\"id\":5,\"head_sha\":\"$1\",\"head_branch\":\"${4:-main}\",\"event\":\"push\",\"path\":\".github/workflows/${5:-build.yml}\",\"head_repository\":{\"full_name\":\"$R\"},\"status\":\"completed\",\"conclusion\":\"${2:-success}\",\"run_attempt\":${3:-1}}]}"
}
# artifact COMMIT DIGEST [IMAGE [HEAD_BRANCH]]: the artifact of the build run (id 5) as the API lists it, and its zip
artifact() {
  echo "{\"artifacts\":[{\"id\":77,\"name\":\"candidate-digest\",\"expired\":false,\"workflow_run\":{\"id\":5,\"head_sha\":\"$1\",\"head_branch\":\"${4:-main}\"}}]}" > "$FX/artifacts"
  printf 'image=%s\ncommit=%s\ndigest=%s\n' "${3:-$IMG}" "$1" "$2" > "$tmp/candidate-digest.txt"
  python3 -I -c 'import sys,zipfile; zipfile.ZipFile(sys.argv[2],"w").write(sys.argv[1],"candidate-digest.txt")' "$tmp/candidate-digest.txt" "$FX/zip"
}
# fixture N CANDIDATE_SHA: the registry holds sha-<candidate> -> D1 (linux/amd64, labelled with the candidate), both workflows green
fixture() {
  FX="$tmp/fx$1"; rm -rf "$FX"; mkdir -p "$FX"
  echo -n "$D1" > "$FX/tag_sha-$2"; printf '%s' "$MANIFEST" > "$FX/manifest"
  echo "{\"os\":\"linux\",\"architecture\":\"amd64\",\"config\":{\"Labels\":{\"org.opencontainers.image.revision\":\"$2\"}}}" > "$FX/cfg_$D1"
  run_json "$2" success 1 main build.yml > "$FX/runs_build.yml"
  artifact "$2" "$D1"
  run_json "$2" success 1 main check.yml > "$FX/runs_check.yml"
}
# expect NAME promote|full PATTERN [COMMIT]
expect() {
  local name=$1 want=$2 pat=$3 commit=${4:-$C1} out rc got
  : > "$tmp/out"
  out=$(PATH="$tmp/bin:$PATH" FX="$FX" IMAGE=$IMG VERSION_TAG=v1.2.3 COMMIT=$commit BUILD_WORKFLOW=build.yml ARTIFACT_NAME=candidate-digest INPUT_PATHS="img" CANDIDATE_WORKFLOWS="build.yml check.yml" TREE_WORKFLOWS="${TREE_WFS:-}" \
        GITHUB_REPOSITORY=$R GH_TOKEN=x GITHUB_OUTPUT="$tmp/out" "$script" 2>&1); rc=$?
  got=$(grep '^promoted=' "$tmp/out" | tail -1)
  if [ "$rc" -ne 0 ] || [ "$got" != "promoted=$([ "$want" = promote ] && echo true || echo false)" ] || ! grep -Eq "$pat" <<<"$out"; then
    echo "FAIL $name: want $want /$pat/, got rc=$rc $got"; sed 's/^/       /' <<<"$out"; failed=1
  elif [ "$want" = full ] && [ -f "$FX/created" ]; then echo "FAIL $name: a tag was created on a full build"; failed=1
  else echo "ok   $name -> $want"; fi
}

fixture 1 "$C1"; expect "tagged commit changed the inputs, candidate green" promote "promoted: candidate sha-${C1:0:7} \($D1\)"
if grep -q "^v1.2.3 ${D1#sha256:}\$" "$FX/created" && grep -q "digest=$D1" "$tmp/out"; then echo "ok   PUT the candidate bytes under the tag, output names the digest"; else echo "FAIL create/output"; failed=1; fi
fixture 2 "$C1"; expect "docs-only commit after the input change uses the older candidate" promote "candidate sha-${C1:0:7}" "$C2"
fixture 3 "$M"; expect "merge commit M is the candidate (first parent, not the side commit)" promote "candidate sha-${M:0:7}" "$C3"
fixture 4 "$S1"; expect "candidate of the side-branch commit is not looked up" full 'no push run|no candidate' "$C3"
fixture 5 "$C1"; rm "$FX/tag_sha-$C1"; expect "no candidate in the registry" full 'no candidate (image|tag)'
fixture 6 "$C1"; touch "$FX/neterr"; expect "registry error" full 'no candidate (image|tag)|cannot'
fixture 7 "$C1"; echo -n application/vnd.oci.image.index.v1+json > "$FX/mt_$D1"; expect "candidate is an index" full 'not a plain image manifest'
fixture 8 "$C1"; sed -i 's/"amd64"/"arm64"/' "$FX/cfg_$D1"; expect "candidate is arm64" full 'not a linux/amd64 image'
fixture 9 "$C1"; sed -i "s/$C1/$C2/" "$FX/cfg_$D1"; expect "revision label names another commit" full 'labelled with revision'
fixture 10 "$C1"; sed -i 's/"Labels":{[^}]*}/"Labels":{}/' "$FX/cfg_$D1"; expect "no revision label" full 'unreadable|labelled with revision'
fixture 11 "$C1"; run_json "$C1" failure 1 > "$FX/runs_build.yml"; expect "candidate run failed" full 'build.yml on .*not a completed first-attempt success'
fixture 12 "$C1"; run_json "$C1" success 2 > "$FX/runs_build.yml"; expect "candidate run green only on a re-run" full 'build.yml on .*not a completed first-attempt'
fixture 13 "$C1"; run_json "$C1" cancelled 1 main check.yml > "$FX/runs_check.yml"; expect "check run cancelled" full 'check.yml on .*not a completed first-attempt'
fixture 14 "$C1"; echo '{"total_count":0,"workflow_runs":[]}' > "$FX/runs_check.yml"; expect "check run missing (still queued or never ran)" full 'check.yml on .*no push run'
fixture 15 "$C1"; run_json "$C1" success 1 feature > "$FX/runs_build.yml"; expect "run on another branch" full 'build.yml on .*no push run'
fixture 16 "$C1"; run_json "$C1" success 1 main other.yml > "$FX/runs_build.yml"; expect "run of another workflow file" full 'build.yml on .*no push run'
fixture 17 "$C1"; run_json "$C2" success 1 > "$FX/runs_build.yml"; expect "run of another commit" full 'build.yml on .*no push run'
fixture 18 "$C1"; rm "$FX/runs_build.yml"; expect "runs API error" full 'cannot list the runs of build.yml'
fixture 19 "$C1"; echo '{"total_count":150,"workflow_runs":[]}' > "$FX/runs_build.yml"; expect "possibly truncated run list" full 'too many runs'
fixture 20 "$C1"; echo 'not json' > "$FX/runs_build.yml"; expect "unreadable run list" full 'unreadable run list'
fixture 21 "$C1"; echo -n "$D1" > "$FX/tag_v1.2.3"; expect "version tag already points at the candidate (re-run)" promote 'promoted'
fixture 22 "$C1"; echo -n "$D2" > "$FX/tag_v1.2.3"; expect "version tag exists with another digest" full 'exists with another digest'
fixture 23 "$C1"; touch "$FX/neterr"; echo -n "$D1" > "$FX/tag_sha-$C1"; expect "registry unreachable" full 'cannot|no candidate'
fixture 24 "$C1"; git -C "$repo" rev-list -1 "$C1" >/dev/null; expect "history without a commit for the inputs" full 'no commit changed the image inputs' "$(git -C "$repo" hash-object -t commit --stdin -w <<<"$(git cat-file commit "$C1" | sed 's/^tree .*/tree 4b825dc642cb6eb9a060e54bf8d69288fbee4904/;/^parent/d')")"
fixture 28 "$C1"; run_json "$C2" success 1 main tree.yml > "$FX/runs_tree.yml"; TREE_WFS=tree.yml expect "tagged tree checked by a green run of its own" promote 'tagged tree checked by: tree.yml' "$C2"
fixture 29 "$C1"; run_json "$C1" success 1 main tree.yml > "$FX/runs_tree.yml"; TREE_WFS=tree.yml expect "tree check ran for the candidate commit only, not the tagged one" full 'tree.yml on .*no push run' "$C2"
fixture 30 "$C1"; run_json "$C2" failure 1 main tree.yml > "$FX/runs_tree.yml"; TREE_WFS=tree.yml expect "tree check of the tagged commit failed" full 'tree.yml on .*not a completed first-attempt' "$C2"
fixture 31 "$C1"; TREE_WFS=tree.yml expect "tree check runs not available" full 'cannot list the runs of tree.yml' "$C2"
fixture 34 "$C1"; echo -n "$D2" > "$FX/tag_sha-$C1"; expect "tag was moved to another digest than the build run recorded" full 'names .*recorded'
fixture 35 "$C1"; artifact "$C1" "$D2"; expect "artifact records another digest than the tag" full 'names .*recorded'
fixture 36 "$C1"; rm "$FX/artifacts"; expect "artifact list unavailable" full 'cannot list the artifacts'
fixture 37 "$C1"; echo '{"artifacts":[]}' > "$FX/artifacts"; expect "missing artifact" full 'no single unexpired artifact'
fixture 38 "$C1"; artifact "$C1" "$D1" "$IMG" feature-x; expect "artifact of a run on another branch (e.g. a pull request)" full 'no single unexpired artifact'
fixture 39 "$C1"; jq '.artifacts[0].workflow_run.id=6' "$FX/artifacts" > "$FX/x" && mv "$FX/x" "$FX/artifacts"; expect "artifact belongs to another run" full 'no single unexpired artifact'
fixture 40 "$C1"; jq '.artifacts[0].expired=true' "$FX/artifacts" > "$FX/x" && mv "$FX/x" "$FX/artifacts"; expect "expired artifact" full 'no single unexpired artifact'
fixture 41 "$C1"; jq '.artifacts[0].workflow_run.head_sha="'"$C2"'"' "$FX/artifacts" > "$FX/x" && mv "$FX/x" "$FX/artifacts"; expect "artifact of another commit" full 'no single unexpired artifact'
fixture 42 "$C1"; artifact "$C1" "$D1" ghcr.io/owner/other; expect "artifact names another image" full 'names another image or commit'
fixture 43 "$C1"; artifact "$C2" "$D1"; jq --arg h "$C1" '.artifacts[0].workflow_run.head_sha=$h' "$FX/artifacts" > "$FX/x" && mv "$FX/x" "$FX/artifacts"; expect "artifact content names another commit" full 'names another image or commit'
fixture 44 "$C1"; artifact "$C1" "not-a-digest"; expect "artifact holds no digest" full 'holds no digest'
fixture 45 "$C1"; echo garbage > "$FX/zip"; expect "artifact is not a zip" full 'no candidate-digest.txt'
fixture 46 "$C1"; jq '.workflow_runs += .workflow_runs | .total_count=2' "$FX/runs_build.yml" > "$FX/x" && mv "$FX/x" "$FX/runs_build.yml"; expect "two push runs of the build workflow" full 'expected exactly 1'
fixture 25 "$C1"; touch "$FX/create_fail"
out=$(PATH="$tmp/bin:$PATH" FX="$FX" IMAGE=$IMG VERSION_TAG=v1.2.3 COMMIT=$C1 BUILD_WORKFLOW=build.yml ARTIFACT_NAME=candidate-digest INPUT_PATHS=img CANDIDATE_WORKFLOWS="build.yml check.yml" TREE_WORKFLOWS="${TREE_WFS:-}" GITHUB_REPOSITORY=$R GH_TOKEN=x "$script" 2>&1); rc=$?
[ "$rc" -eq 1 ] && grep -q 'tagging .* failed' <<<"$out" && echo "ok   PUT fails -> error exit 1, no fall back" || { echo "FAIL create failure rc=$rc"; failed=1; }
fixture 26 "$C1"; echo -n "$D2" > "$FX/create_other"
out=$(PATH="$tmp/bin:$PATH" FX="$FX" IMAGE=$IMG VERSION_TAG=v1.2.3 COMMIT=$C1 BUILD_WORKFLOW=build.yml ARTIFACT_NAME=candidate-digest INPUT_PATHS=img CANDIDATE_WORKFLOWS="build.yml check.yml" TREE_WORKFLOWS="${TREE_WFS:-}" GITHUB_REPOSITORY=$R GH_TOKEN=x "$script" 2>&1); rc=$?
[ "$rc" -eq 1 ] && grep -q 'after the promotion, expected' <<<"$out" && echo "ok   digest differs after the promotion -> error exit 1" || { echo "FAIL digest check rc=$rc"; failed=1; }
fixture 32 "$C1"; printf 'tampered' > "$FX/manifest"; expect "fetched manifest does not hash to the digest" full 'does not hash to'
fixture 33 "$C1"; touch "$FX/token_fail"; expect "no registry token with push access" full 'no registry token'
fixture 27 "$C1"; out=$(PATH="$tmp/bin:$PATH" FX="$FX" IMAGE=$IMG VERSION_TAG=v1.2.3 COMMIT=nothex BUILD_WORKFLOW=build.yml ARTIFACT_NAME=candidate-digest INPUT_PATHS=img CANDIDATE_WORKFLOWS=build.yml GITHUB_REPOSITORY=$R GH_TOKEN=x "$script" 2>&1)
grep -q 'bad commit' <<<"$out" && echo "ok   malformed commit -> full build" || { echo "FAIL bad commit"; failed=1; }

# missing inputs and bad version tags fall back to a full build instead of aborting
for miss in GH_TOKEN GITHUB_SHA IMAGE; do
  fixture 5$miss "$C1"; : > "$tmp/out"
  out=$(env -i PATH="$tmp/bin:$PATH" HOME="$tmp" FX="$FX" IMAGE=$IMG VERSION_TAG=v1.2.3 COMMIT=$C1 GITHUB_SHA=$C1 BUILD_WORKFLOW=build.yml ARTIFACT_NAME=candidate-digest INPUT_PATHS=img CANDIDATE_WORKFLOWS=build.yml GITHUB_REPOSITORY=$R GH_TOKEN=x GITHUB_OUTPUT="$tmp/out" \
        bash -c 'unset "$0"; [ "$0" != GITHUB_SHA ] || unset COMMIT; exec "$1"' "$miss" "$script" 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && grep -q 'promoted=false' "$tmp/out" && grep -q "missing input:" <<<"$out"; then echo "ok   missing $miss -> full build, exit 0"; else echo "FAIL missing $miss rc=$rc: $out"; failed=1; fi
done
for badv in 'v1.2.3/../x' 'latest' 'v1.2' 'v1.2.3 '; do
  fixture 6x "$C1"
  out=$(PATH="$tmp/bin:$PATH" FX="$FX" IMAGE=$IMG VERSION_TAG="$badv" COMMIT=$C1 BUILD_WORKFLOW=build.yml ARTIFACT_NAME=candidate-digest INPUT_PATHS=img CANDIDATE_WORKFLOWS=build.yml GITHUB_REPOSITORY=$R GH_TOKEN=x GITHUB_OUTPUT="$tmp/out" "$script" 2>&1); rc=$?
  if [ "$rc" -eq 0 ] && grep -q 'does not match' <<<"$out" && [ ! -f "$FX/created" ]; then echo "ok   version tag '$badv' refused -> full build"; else echo "FAIL version tag '$badv' rc=$rc: $out"; failed=1; fi
done
# the token never appears in a curl argument list (the stub records its argv)
fixture 70 "$C1"; : > "$tmp/curl-argv"
CURL_ARGV="$tmp/curl-argv" expect "promotion with an argv-recording curl" promote 'promoted'
if grep -q 'x-access-token\|Bearer' "$tmp/curl-argv"; then echo "FAIL a credential is in a curl argument list"; failed=1; else echo "ok   no credential in curl arguments"; fi
[ "$failed" = 0 ] && echo "all ci-promote scenarios passed" || { echo "SOME FAILED"; exit 1; }
