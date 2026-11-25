#!/usr/bin/env bash
# Offline test of scripts/ci-select-tests.sh: a stub `gh` answers the labels of the pull request, small git repositories
# hold a base commit and the change of a pull request, and the real scripts/select_tests.py runs as the selector.
# Every way the decision can go wrong must end in "full". Also checks that check.yml keeps its side of the contract.
# Usage: tests/ci-select/run.sh   (needs git and python3)
set -u
here=$(cd "$(dirname "$0")" && pwd)
root=$here/../..
script=$root/scripts/ci-select-tests.sh
selector=$root/scripts/select_tests.py
workflow=$root/.github/workflows/check.yml
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
failed=0
G=(git -c user.name=t -c user.email=t@t -c commit.gpgsign=false)

mkdir -p "$tmp/bin"
cat > "$tmp/bin/gh" <<'STUB'
#!/usr/bin/env bash
# gh api repos/owner/repo/pulls/7 --jq '.labels[].name'
[ "$1" = api ] && [ "$2" = repos/owner/repo/pulls/7 ] && [ -z "${GH_FAIL:-}" ] || exit 1
cat "$LABELS_FILE"
STUB
chmod +x "$tmp/bin/gh"

# A repository with 19 test files: three guards, the four documentation checks, two that import a module of the package
# and ten that import nothing. $1: a directory; $2: a command that edits the base before its commit (default none).
make_base() {
  local d=$1 i
  rm -rf "$d"; mkdir -p "$d/scripts" "$d/packages/pkg/src/pkg" "$d/tests" "$d/docs"; cd "$d" || exit 1
  "${G[@]}" init -q -b main
  cp "$selector" scripts/select_tests.py
  printf '[project]\nname = "pkg"\nversion = "0"\n' > packages/pkg/pyproject.toml
  : > packages/pkg/src/pkg/__init__.py; echo 'X = 1' > packages/pkg/src/pkg/a.py; echo 'X = 2' > packages/pkg/src/pkg/b.py
  : > tests/conftest.py
  for t in architecture source_rules vocabulary_leak docs doc_facts doc_claims doc_sets; do echo 'def test_it() -> None: pass' > "tests/test_$t.py"; done
  echo 'from pkg import a' > tests/test_a.py; echo 'from pkg import b' > tests/test_b.py
  for i in 1 2 3 4 5 6 7 8 9 10; do echo 'def test_it() -> None: pass' > "tests/test_filler_$i.py"; done
  echo '# Guide' > docs/guide.md
  [ -z "${2:-}" ] || eval "$2"
  "${G[@]}" add -A && "${G[@]}" commit -q -m base
  BASE=$("${G[@]}" rev-parse HEAD)
}

# expect NAME full|selected PATTERN [EVENT]  (run in the repository of make_base, after the change is committed)
LABELS=""; PR=7
expect() {
  local name=$1 want=$2 pat=$3 ev=${4:-pull_request} out rc
  printf '%s' "$LABELS" > "$tmp/labels"; : > "$tmp/gh_out"
  out=$(PATH="$tmp/bin:$PATH" LABELS_FILE="$tmp/labels" GITHUB_EVENT_NAME=$ev GITHUB_REPOSITORY=owner/repo BASE_SHA=$BASE PR_NUMBER=$PR \
        GH_TOKEN=x GITHUB_OUTPUT="$tmp/gh_out" GITHUB_STEP_SUMMARY="$tmp/summary" CI_SELECT_PYTHON=python3 "$script" 2>&1); rc=$?
  local mode; mode=$(sed -n 's/^mode=//p' "$tmp/gh_out")
  if [ "$rc" -ne 0 ] || [ "$mode" != "$want" ] || ! grep -Eq "$pat" <<<"$out"; then
    echo "FAIL $name: want $want /$pat/, got rc=$rc mode=$mode"; sed 's/^/       /' <<<"$out"; failed=1
  else echo "ok   $name -> $want"; fi
}
args() { sed -n "s/^args=//p" "$tmp/gh_out"; }
# check NAME CONDITION-COMMAND...: the command must succeed
check() { local name=$1; shift; if "$@"; then echo "ok   $name"; else echo "FAIL $name"; failed=1; fi; }
pr() { git checkout -q -b "pr$RANDOM" "$BASE" && eval "$1" && "${G[@]}" add -A && "${G[@]}" commit -q -m change; }

d=$tmp/r1; make_base "$d"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "module change selects what imports it" selected '4 test files selected'
check "  ... the module's test and the guards, nothing else" bash -c 'a=" $(sed -n "s/^args=//p" "'"$tmp"'/gh_out") "; [[ $a == *" tests/test_a.py "* && $a == *" tests/test_architecture.py "* && $a == *" tests/test_source_rules.py "* && $a == *" tests/test_vocabulary_leak.py "* && $a != *test_b.py* && $a != *filler* ]]'
check "  ... the summary shows the selector output" grep -q 'Selector output' "$tmp/summary"
expect "push to main" full 'not a pull request \(push\)' push
LABELS=$'bug\nci:full\n'; expect "label ci:full" full 'label ci:full on #7'
LABELS=$'bug\nci:fuller\n'; expect "another label that contains the name" selected 'test files selected'
LABELS=""
GH_FAIL=1 expect "API error reading the labels" full 'cannot read the labels'
PR=""; expect "no pull request number" full 'no pull request number'; PR=7
BASE_SAVED=$BASE; BASE=dead; expect "no base commit" full 'no base commit'; BASE=$(printf '1%.0s' {1..40}); expect "base commit not in the checkout" full 'not in the checkout'; BASE=$BASE_SAVED

d=$tmp/r2; make_base "$d"
(pr "echo '# Guide, changed' > docs/guide.md")
expect "a document selects the documentation checks" selected 'test files selected'
check "  ... they are in the selection" bash -c 'a=" $(sed -n "s/^args=//p" "'"$tmp"'/gh_out") "; [[ $a == *" tests/test_docs.py "* && $a == *" tests/test_doc_claims.py "* ]]'

d=$tmp/r3; make_base "$d"
(pr "printf '[project]\nname = \"pkg\"\nversion = \"1\"\n' > packages/pkg/pyproject.toml; printf 'import sys\nprint(\"tests/test_a.py\")\n' > scripts/select_tests.py")
expect "a change cannot replace the selector that judges it (the base's runs, the pull request's would print a file)" full 'the selector says full: packages/pkg/pyproject.toml'

d=$tmp/r4; make_base "$d" "rm scripts/select_tests.py"
(pr "cp '$selector' scripts/select_tests.py; echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "the base has no selector yet (the pull request adds it)" full 'has no scripts/select_tests.py yet'

d=$tmp/r5; make_base "$d" "printf 'print(\"tests/test_a.py\")\nprint(\"--help\")\n' > scripts/select_tests.py"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "the selector prints something that is no test path" full 'not a test path'
d=$tmp/r6; make_base "$d" "printf 'print(\"tests/x y.py\")\n' > scripts/select_tests.py"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "a path with a space" full 'not a test path'
d=$tmp/r7; make_base "$d" "printf 'print(\"tests/test_a.py; echo x\")\n' > scripts/select_tests.py"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "a path with a shell command" full 'not a test path'
d=$tmp/r8; make_base "$d" "printf 'import sys\nsys.exit(3)\n' > scripts/select_tests.py"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "the selector fails" full 'the selector failed \(exit 3\)'
d=$tmp/r9; make_base "$d" ": > scripts/select_tests.py"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "the selector prints nothing" full 'printed nothing'
d=$tmp/r10; make_base "$d" "printf 'print(\"tests/test_a.py\")\n' > scripts/select_tests.py"
(pr "echo 'X = 3' > packages/pkg/src/pkg/a.py")
expect "a selector that answers with one path" selected '1 test files selected'
check "  ... args hold that path" test "$(args)" = tests/test_a.py

d=$tmp/r11; make_base "$d"
(pr "echo 'X = 3' > .gitignore")
expect "a change the selector answers full to" full 'the selector says full'

d=$tmp/r12; make_base "$d"
check "no changed files" bash -c "cd '$d' && git checkout -q $BASE && printf '%s' '' > '$tmp/labels'; PATH='$tmp/bin:$PATH' LABELS_FILE='$tmp/labels' GITHUB_EVENT_NAME=pull_request GITHUB_REPOSITORY=owner/repo BASE_SHA=$BASE PR_NUMBER=7 GITHUB_OUTPUT='$tmp/o' CI_SELECT_PYTHON=python3 '$script' | grep -q 'no changed files'"

# check.yml keeps its side: the whole suite is the default, the selection comes from the script (and so from the base),
# and a push to main reuses a pull request run only when it ran the whole suite.
cd "$root" || exit 1
step() { awk -v n="      - name: $1" '$0 == n {on=1; print; next} on && /^      - /{exit} on {print}' "$workflow"; }
check "check.yml does not call select_tests.py itself (only scripts/ci-select-tests.sh runs the base's version)" bash -c "! grep -v '^ *#' '$workflow' | grep -q select_tests.py"
check "ci-select-tests.sh runs the selector from the base commit" grep -qF 'git show "$base:scripts/select_tests.py"' "$script"
check "ci-select-tests.sh never runs the selector of the checkout" bash -c "! grep -v '^ *#' '$script' | grep -E 'python[^ ]* (-I )?(\\./)?scripts/select_tests.py|/src/scripts/select_tests.py'"
check "the full suite step runs unless the selection says selected" bash -c "$(declare -f step); workflow='$workflow'; step 'Test suite (full)' | grep -qF \"steps.select.outputs.mode != 'selected'\""
check "the full suite step passes no selection" bash -c "$(declare -f step); workflow='$workflow'; ! step 'Test suite (full)' | grep -q 'build-args'"
check "the selected suite step runs only when the selection says selected" bash -c "$(declare -f step); workflow='$workflow'; step 'Test suite (selected)' | grep -qF \"steps.select.outputs.mode == 'selected'\" && step 'Test suite (selected)' | grep -qF 'PYTEST_ARGS=\${{ steps.select.outputs.args }}'"
check "the reuse rule names the whole-suite step" grep -qxF "  CI_REUSE_REQUIRE: 'test/Test suite (full)'" "$workflow"
check "the reuse rule refuses a pull request that changed the CI itself" grep -qE "^  CI_REUSE_REFUSE_PATHS: .*check.*ci-.*Dockerfile" "$workflow"
check "the test job is named test" bash -c "grep -q '^  test:\$' '$workflow'"
check "only pull requests can select: the selection step sees the event" bash -c "$(declare -f step); workflow='$workflow'; step 'Select the tests' | grep -qF 'run: scripts/ci-select-tests.sh'"
check "the Dockerfile test stage takes PYTEST_ARGS and defaults to the whole suite" bash -c "grep -qx 'ARG PYTEST_ARGS=\"\"' '$root/Dockerfile' && grep -qF 'RUN uv run pytest -q -p no:cacheprovider -n 8 \${PYTEST_ARGS}' '$root/Dockerfile'"

exit "$failed"
