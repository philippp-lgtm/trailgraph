#!/bin/sh
# Smoke test of a runtime image (CI: check.yml and build.yml): the container starts,
# answers /v1/health and `graph-admin audit verify` of its fresh data root is clean.
#
#   scripts/smoke_image.sh <image>
set -eu
image=$1
name="graph-server-smoke-$$"

health() {
  docker exec "$name" python -c \
    "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8470/v1/health', timeout=4).read().decode())"
}

docker run -d --name "$name" "$image" >/dev/null
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT
# Up to 3 minutes: an image of another architecture starts under emulation.
ok=0
for _ in $(seq 90); do
  [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ] || break
  if health >/dev/null 2>&1; then ok=1; break; fi
  sleep 2
done
if [ "$ok" -ne 1 ]; then
  echo "smoke: $image did not answer /v1/health" >&2
  docker logs "$name" >&2 || true
  exit 1
fi
health
docker exec "$name" graph-admin audit verify
