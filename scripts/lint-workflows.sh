#!/usr/bin/env bash
# lint-workflows.sh - static check of every workflow and local composite action (infra DECISIONS.md I32): `bash -n` over every
# `run:` block (the ${{ }} expressions replaced by a word), then actionlint (expressions, contexts, job graph; its shellcheck
# integration is off, the repositories do not shellcheck inline scripts). Jobs that only run on push or tag events are never
# exercised by a pull request, so a syntax error there would otherwise surface on the first merge or release.
# Vendored byte-identical into website, trailgraph and microservices. Needs yq, curl, tar, sha256sum.
set -eu
cd "$(dirname "$0")/.."
# Fail closed: a missing tool, an unreadable file or zero run blocks must never look like a clean lint.
for tool in yq curl tar sha256sum base64; do command -v "$tool" >/dev/null || { echo "FAIL: $tool is not installed" >&2; exit 1; }; done
ACTIONLINT_VERSION=1.7.12
case "$(uname -m)" in
  x86_64) arch=amd64; sum=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 ;;
  aarch64) arch=arm64; sum=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6 ;;
  *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

files=$(ls .github/workflows/*.yml .github/actions/*/action.yml 2>/dev/null || true)
fail=0; n=0
[ -n "$files" ] || { echo "FAIL: no workflow found" >&2; exit 1; }
for f in $files; do
  blocks=$(yq -r '[.jobs[]?.steps[]?.run, .runs.steps[]?.run] | .[] | select(. != null) | @base64' "$f") \
    || { echo "FAIL $f: yq cannot read it" >&2; fail=1; continue; }
  while IFS= read -r b64; do
    [ -n "$b64" ] || continue
    n=$((n + 1))
    # a "}" inside an expression would end the match early; harmless, the rest of the block is still parsed
    printf '%s' "$b64" | base64 -d | sed 's/\${{[^}]*}}/X/g' > "$tmp/run.sh"
    if ! err=$(bash -n "$tmp/run.sh" 2>&1); then echo "FAIL $f: run block does not parse: $err" >&2; fail=1; fi
  done <<<"$blocks"
done
echo "bash -n: $n run blocks in $(wc -w <<<"$files") files"
[ "$n" -gt 0 ] || { echo "FAIL: no run block found; the check would prove nothing" >&2; exit 1; }

curl -fsSL -o "$tmp/al.tgz" "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_${arch}.tar.gz"
echo "$sum  $tmp/al.tgz" | sha256sum -c - >/dev/null
tar -xzf "$tmp/al.tgz" -C "$tmp" actionlint
# actionlint checks workflow files only; composite actions get the bash -n pass above and nothing more.
# shellcheck disable=SC2086
"$tmp/actionlint" -shellcheck= -pyflakes= -color $(ls .github/workflows/*.yml) || fail=1
exit "$fail"
