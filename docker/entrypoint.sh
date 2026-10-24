#!/bin/sh
# First start: create the data root and install the default installation policy
# (the server entrypoint may write every graph, a request without a project
# scope may read). Then serve. Maintenance: docker compose exec graph graph-admin ...
set -eu

graph-admin init --json >/dev/null
if [ ! -e "$XDG_DATA_HOME/graph_cli/policy/installation.json" ]; then
  graph-admin policy apply /usr/local/share/graph_cli/default_policy.yaml \
    --reason "container first start: default installation policy" --json >/dev/null
fi
# Inside the container network only; compose publishes the port to 127.0.0.1
# or puts the TLS proxy in front.
exec graph-server serve --host 0.0.0.0 --port 8470 --allow-plain-http "$@"
