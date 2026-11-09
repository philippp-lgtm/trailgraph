# Erase data (purge)

When the user asks to withdraw data for good (personal data on request, a
document past its retention period), purge the entities. Only a maintainer
can, and only on a sealed graph (`admin.graph_not_sealed` otherwise: the
graph stores its values in the clear and a purge cannot erase them; tell the
user).

1. Find the entities with the `graph-cli` skill; note their ids or aliases and
   which `files:File` entities belong to them.
2. If a relation itself must go, unlink it first with the `graph-cli` skill: a
   purge keeps ids, types and edges, and erases only values.
3. `graph-admin graph purge <graph> <entity> --reason "<ticket>" --dry-run`
   (add the file entities and `--with-files` to delete their stored files).
   Read `retracted`, `keys` and `blobs_removed`, and confirm with the user.
4. Run it again without `--dry-run`. The reason stays readable forever: a
   ticket number, never the person's name.
5. `graph-admin audit verify`. Tell the user that copies of the key store from
   before the purge still hold the keys until they are gone.

An interrupted purge is completed by running the same command again;
`integrity.keys.blob_not_removed` in `audit verify` means exactly that. What
a purge erases and what it keeps (ids, types, edges, the audit trail) is in
`graph-admin graph purge --help`.
