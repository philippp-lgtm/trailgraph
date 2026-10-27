#!/usr/bin/env bash
# Create the example graph "company" in the installation `graph` and `graph-admin` talk to
# (a local data root, or a server in remote mode), fill it with the seed data and check it.
#
#   examples/company/load.sh            both versions, the skill, the checks
#   examples/company/load.sh 1.0.0      stop after version 1.0.0 and its seeds
#
# Needs `graph` and `graph-admin` on PATH and the right to maintain graphs. The seeds are
# applied with `graph apply`, so the current directory has to be one where `graph` may write
# the graph "company" (a project directory whose pointer lists it, in remote mode). See the
# README next to this script. For a throwaway installation use setup.sh instead.
set -euo pipefail

upto="${1:-1.1.0}"
case "$upto" in 1.0.0 | 1.1.0) ;; *)
  echo "usage: $0 [1.0.0|1.1.0]" >&2
  exit 2
  ;;
esac

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
graph_name=company

graph-admin graph create "$graph_name" --ontology "$here/ontology/1.0.0" --module company \
  --title "Example company: people, organization, operations and legal entities" --language en
graph apply "$here/seeds/1.0.0.jsonl" --graph "$graph_name"
graph-admin graph check "$graph_name"

if [ "$upto" = 1.1.0 ]; then
  graph-admin ontology release "$graph_name" "$here/ontology/1.1.0" --module company
  graph apply "$here/seeds/1.1.0.jsonl" --graph "$graph_name"
  graph-admin graph skill publish "$graph_name" "$here/skill/graph-company" \
    --reason "Curated skill of the example graph"
  graph-admin graph check "$graph_name"
fi

graph-admin audit verify
