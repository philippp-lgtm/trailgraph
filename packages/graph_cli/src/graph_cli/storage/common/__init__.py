"""Backend-neutral storage core shared by every adapter.

Chained JSONL codec, fault points, commit checks, the tx-log replay view and
the cipher primitives of sealed graphs (``crypto``).
The reference query evaluator lives in ``graph_cli.kernel.query_eval``.
Adapters (``memory``, ``files``) build on it and stay independent of each
other.
"""
