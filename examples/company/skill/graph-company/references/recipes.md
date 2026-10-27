# More recipes

Replace the placeholders. Every command passes `--graph company`.

## Reading

Everything around one entity, two hops out:

```sh
graph neighbors <id> --depth 2 --graph company
```

Open tasks, the next due first:

```sh
graph find --type ops:Task --where status=open --sort due_on --graph company
```

Employees and externals who hold a position of one unit, through the position:

```sh
graph query 'MATCH (p:Person)-[:holds]->(q:Position)-[:in_unit]->(u) WHERE u.name = $unit RETURN p.name, q.title' --graph company --param unit="<unit name>"
```

## Writing

Check a state change before making it; the answer names the guard that would refuse it:

```sh
graph transition <id> <state> --graph company --dry-run
```

Record who holds a position, with the date the person took over:

```sh
graph link <person id> org:holds <position id> --qualifier since=<date> --graph company
```
