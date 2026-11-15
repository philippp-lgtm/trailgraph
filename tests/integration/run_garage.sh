#!/usr/bin/env bash
# run_garage.sh - the object store integration test (tests/integration/test_s3_garage.py) against a real Garage.
#
# Starts the pinned Garage image as a single node (replication_factor 1, data and metadata fsync, region "garage",
# path-style S3 only), applies a layout, creates a bucket, imports a throwaway key with read and write on it, sets a
# small size quota, then runs pytest on tests/integration with the GRAPH_CLI_TEST_S3_* variables set. Everything it
# started (container, network) is removed on exit, also on failure. Needs only Docker and, without --container, uv.
#
# Usage: tests/integration/run_garage.sh [--container IMAGE] [PYTEST_ARGS...]
#   default            Garage's S3 port is published on 127.0.0.1 and pytest runs here with `uv run --frozen`
#   --container IMAGE  pytest runs in IMAGE (a uv image) on Garage's Docker network with the checkout mounted, as the
#                      calling user (CI: the runner has nothing but Docker); the endpoint is then plain http:// to a
#                      host that is not loopback, which the test allows with [files.s3] allow_plain_http
set -euo pipefail

GARAGE_IMAGE="docker.io/dxflrs/garage:v2.5.0@sha256:677e5b758e4425a325b811c425c026bf35a4bf42f7ddbaa773de3f4fd779a5a5"
BUCKET="graph-cli-test"
QUOTA_BYTES=$((16 * 1024 * 1024))   # small enough for the test to exceed with one file below [files] max_bytes

repo=$(cd "$(dirname "$0")/../.." && pwd)
image=""
if [ "${1:-}" = "--container" ]; then
  [ -n "${2:-}" ] || { echo "usage: $0 [--container IMAGE] [PYTEST_ARGS...]" >&2; exit 2; }
  image=$2
  shift 2
fi

name="graph-cli-garage-$$-$RANDOM"
tmp=$(mktemp -d)
cleanup() {
  docker rm -f -v "$name" >/dev/null 2>&1 || true
  docker network rm "$name" >/dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT

hex() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# Throwaway values of this run only, never credentials of a real store.
cat > "$tmp/garage.toml" <<EOF
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
data_fsync = true
metadata_fsync = true
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "$(hex 32)"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
EOF
chmod 0644 "$tmp/garage.toml"
key_id="GK$(hex 12)"
secret=$(hex 32)

docker network create "$name" >/dev/null
publish=()
[ -n "$image" ] || publish=(-p 127.0.0.1::3900)
# Copied in rather than mounted: a runner's Docker daemon may not see this machine's /tmp.
docker create --name "$name" --network "$name" --network-alias garage "${publish[@]}" --memory 512m \
  "$GARAGE_IMAGE" >/dev/null
docker cp "$tmp/garage.toml" "$name:/etc/garage.toml" >/dev/null
docker start "$name" >/dev/null

garage() { docker exec -e RUST_LOG=warn "$name" /garage "$@"; }
node=""
for _ in $(seq 1 30); do
  node=$(garage node id -q 2>/dev/null) && break
  sleep 1
done
[ -n "$node" ] || { echo "Garage did not start:" >&2; docker logs "$name" 2>&1 | tail -20 >&2; exit 1; }
garage layout assign -z test -c 1G "${node%%@*}" >/dev/null
garage layout apply --version 1 >/dev/null
garage bucket create "$BUCKET" >/dev/null
garage key import --yes -n graph-cli-test "$key_id" "$secret" >/dev/null
garage bucket allow --read --write "$BUCKET" --key "$key_id" >/dev/null
garage bucket set-quotas "$BUCKET" --max-size "$QUOTA_BYTES" >/dev/null
echo "Garage ready: bucket $BUCKET, quota $QUOTA_BYTES bytes"

export GRAPH_CLI_TEST_S3_BUCKET="$BUCKET" GRAPH_CLI_TEST_S3_REGION=garage \
  GRAPH_CLI_TEST_S3_ACCESS_KEY_ID="$key_id" GRAPH_CLI_TEST_S3_SECRET_ACCESS_KEY="$secret" \
  GRAPH_CLI_TEST_S3_QUOTA_BYTES="$QUOTA_BYTES"
status=0
if [ -z "$image" ]; then
  port=$(docker port "$name" 3900/tcp | head -1 | sed 's/.*://')
  export GRAPH_CLI_TEST_S3_ENDPOINT="http://127.0.0.1:$port"
  (cd "$repo" && env -u CLAUDE_AGENT_ID -u CLAUDE_AGENT_TYPE -u CLAUDE_SESSION_ID \
    uv run --frozen pytest -p no:cacheprovider tests/integration "$@") || status=$?
else
  export GRAPH_CLI_TEST_S3_ENDPOINT="http://garage:3900"
  docker run --rm --network "$name" --user "$(id -u):$(id -g)" -v "$repo:/src" -w /src \
    -e HOME=/tmp -e UV_PROJECT_ENVIRONMENT=/tmp/venv -e UV_PYTHON_DOWNLOADS=never -e UV_LINK_MODE=copy \
    -e GRAPH_CLI_TEST_S3_ENDPOINT -e GRAPH_CLI_TEST_S3_BUCKET -e GRAPH_CLI_TEST_S3_REGION \
    -e GRAPH_CLI_TEST_S3_ACCESS_KEY_ID -e GRAPH_CLI_TEST_S3_SECRET_ACCESS_KEY -e GRAPH_CLI_TEST_S3_QUOTA_BYTES \
    "$image" sh -euc 'uv sync --frozen --all-packages; uv run pytest -p no:cacheprovider tests/integration "$@"' \
    pytest "$@" || status=$?
fi
[ "$status" = 0 ] || { echo "Garage log (last lines):" >&2; docker logs "$name" 2>&1 | tail -20 >&2; }
exit "$status"
