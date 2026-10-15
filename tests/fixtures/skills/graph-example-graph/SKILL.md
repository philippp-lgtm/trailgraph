---
name: graph-example-graph
description: "Items, their locations and their stock in the test graph example_graph. Use when a test asks what is stored where. Needs the graph-cli skill."
---

# Graph `example_graph`: items and locations

Read the `graph-cli` skill first. This skill says what `example_graph` holds.

## What is in it

- `inv:Item`: a tracked thing with a `name` and a `quantity`.
- `inv:Location`: where items are kept; items point to it with
  `inv:located_in`.

## Recipes

- Items in one place: `graph find --graph example_graph --type inv:Item --rel inv:located_in=<id> --json`.
- Details: [references/model.md](references/model.md).
