# syntax=docker/dockerfile:1
# graph-server in a container that owns the data (docs/server.md#container).
#
#   docker build --target test .   the whole test suite on Linux
#   docker compose up -d           the server on 127.0.0.1:8470

ARG PYTHON=python:3.13-slim
ARG UV=ghcr.io/astral-sh/uv:0.13

FROM ${UV} AS uv

# ---- build: wheels of the three distributions, pinned third-party requirements ----
# Each wheel carries its build id (scripts/build_id.py): a client installed from the same
# tree names the same one, and the server refuses another (docs/server.md). /dist keeps
# the client wheels and their pins (without hashes: a client on another platform resolves
# its own wheels of them); the server hands them out under /v1/dist/.
FROM ${PYTHON} AS build
COPY --from=uv /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY scripts/build_id.py ./scripts/
RUN python scripts/build_id.py \
 && uv build --package graph_cli --wheel --out-dir /wheels \
 && uv build --package graph_cli_admin --wheel --out-dir /wheels \
 && uv build --package graph_cli_server --wheel --out-dir /wheels \
 && uv export --frozen --no-dev --no-emit-workspace --package graph_cli_server \
      --package graph_cli_admin --format requirements-txt -o /wheels/requirements.txt \
 && mkdir /dist && cp /wheels/graph_cli-*.whl /wheels/graph_cli_admin-*.whl /dist/ \
 && uv export --frozen --no-dev --no-emit-workspace --no-hashes --no-header \
      --package graph_cli_admin --format requirements-txt -o /dist/constraints.txt

# ---- test: the whole suite on Linux (flock, the fsync path, temp dirs) ----
FROM ${PYTHON} AS test
COPY --from=uv /uv /bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/venv
RUN useradd --create-home tester && mkdir /venv /src && chown tester /venv /src
WORKDIR /src
COPY --chown=tester . .
USER tester
RUN uv sync --frozen --all-packages
# Eight workers on a four-core sidecar: the suite spends much of its time waiting on child
# processes, so more workers than cores keep the cores busy (measured: -n 4 5.5 min, -n 8 3.3 min).
RUN uv run pytest -q -p no:cacheprovider -n 8

# ---- runtime: wheels only, a user without a login, data in volumes ----
# pip goes after the install: nothing runs it here, and its vendored packages
# (urllib3, msgpack, setuptools) carry the image's only fixable CVEs.
FROM ${PYTHON} AS runtime
RUN useradd --system --uid 10001 --home-dir /state --shell /usr/sbin/nologin graph \
 && mkdir /data /state /config /workspace \
 && chown graph:graph /data /state /config
COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --require-hashes -r /wheels/requirements.txt \
 && pip install --no-cache-dir --no-deps /wheels/*.whl \
 && pip uninstall --yes pip \
 && rm -rf /wheels
COPY docker/entrypoint.sh /usr/local/bin/graph-container
COPY docker/default_policy.yaml /usr/local/share/graph_cli/default_policy.yaml
COPY --from=build /dist /usr/local/share/graph_cli/dist
ENV HOME=/state XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data XDG_STATE_HOME=/state \
    GRAPH_CLI_DIST_DIR=/usr/local/share/graph_cli/dist PYTHONUNBUFFERED=1
USER graph
VOLUME ["/data", "/state", "/config"]
EXPOSE 8470
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8470/v1/health', timeout=4)"]
ENTRYPOINT ["graph-container"]
