"""Named read functions: the authored format, the compiled catalog and parameter binding.

``format`` reads one function file (``graph_cli.function/1``) into typed
definitions, ``compile`` checks them against a compiled ontology and writes
the catalog document (``graph_cli.functions/1``), ``bind`` turns CLI strings
and JSON into typed query parameters. Pure functions over documents: no
storage, no services. Imports only the kernel, the ontology layer and the
query model (import contract).
"""
