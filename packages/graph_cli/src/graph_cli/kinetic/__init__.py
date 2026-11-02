"""Named read functions and write actions: authored formats, compiled catalogs, binding.

``format`` reads one function file (``graph_cli.function/1``) into typed
definitions, ``compile`` checks them against a compiled ontology and writes
the catalog document (``graph_cli.functions/1``), ``params`` turns CLI strings
and JSON into typed parameter values for both kinds. ``action_format``,
``action_compile`` and ``action_entries`` do the same for actions
(``graph_cli.action/1``, catalog ``graph_cli.action_catalog/1``), and
``expand`` checks an action's preconditions and expands it into a change set
over a snapshot. Pure functions over documents: no
storage, no services. Imports only the kernel, the ontology layer and the
query model (import contract).
"""
