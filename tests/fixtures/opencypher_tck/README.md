# openCypher TCK subset

Scenarios of the openCypher Technology Compatibility Kit, vendored unchanged
for the tests of `graph query` (`tests/unit/query/test_cypher_tck.py`).

- Source: <https://github.com/opencypher/openCypher>, directory `tck/features`,
  commit `677cbafabb8c3c5eed458fd3b1ec0daec8d67d23`.
- License: Apache License 2.0 (`LICENSE`); attribution notice in `NOTICE`
  and in the header of every feature file.
- Files: `features/clauses/` (`match`, `match-where`, `return`,
  `return-orderby`, `return-skip-limit`, `unwind`, `with`, `with-orderBy`,
  `with-skip-limit`, `with-where`) and `features/expressions/`
  (`aggregation`, `boolean`, `comparison`, `conditional`, `null`): the
  clauses and expressions the read-only subset supports.

graph_cli implements a read-only subset of Cypher; it is not an openCypher
implementation approved by the openCypher Implementers Group. The runner
(`tests/support/tck.py`) builds a model per scenario from the labels,
relationship types and property keys it uses. The test skips, with the
reason in the report, scenarios whose setup is not a plain `CREATE` or uses
properties named like graph_cli system fields (`id`, `type`), and scenarios
that use a construct the subset refuses; documented differences (CURIEs
from `labels()` and `type()`, no NaN, `WHERE` after `WITH DISTINCT`) run as
strict xfail.

To update: replace `features/` with the same directories of a newer commit,
update the commit above and the skip lists in the test.
