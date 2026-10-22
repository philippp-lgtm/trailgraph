"""``graph query``: a read-only openCypher subset over the Snapshot port.

``lexer`` and ``parser`` turn the text into the syntax tree of ``ast``.
Imports only the kernel and the ontology layer (import contract): storage
stays unaware of Cypher, and the flag planner ``query.plan`` stays an
independent implementation that the equivalence tests compare against.
"""
