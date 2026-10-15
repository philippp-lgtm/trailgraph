---
name: graph-example-graph
description: "Items, their locations and their stock in the test graph example_graph, rendered from its template. Use when a test asks what is stored where. Needs the graph-cli skill."
---

# Graph `example_graph`: items and locations

## Is it available here?

<!-- graph_cli:block start -->

## What is in it

<!-- graph_cli:block types -->

<!-- graph_cli:block relations -->

<!-- graph_cli:block constraints -->

<!-- graph_cli:block model -->

## Rules for you

- Every `inv:Item` is stored somewhere: create it with its `inv:located_in`
  edge in the same call.
- Preview every write with `--dry-run` first.

## Reading

<!-- graph_cli:block names -->

```sh
graph find --type inv:Item --rel inv:located_in=LOC-0001 --graph example_graph --json
graph neighbors ITEM-0001 --rel inv:part_of --direction out --graph example_graph --json
```

More recipes: [references/recipes.md](references/recipes.md).
