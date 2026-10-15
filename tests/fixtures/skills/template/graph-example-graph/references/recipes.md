# Recipes for `example_graph`

Create an item in one place (preview first):

```sh
graph create inv:Item --set name="Spare cable" --set quantity=2 \
  --edge inv:located_in=LOC-0001 --graph example_graph --dry-run --json
```
