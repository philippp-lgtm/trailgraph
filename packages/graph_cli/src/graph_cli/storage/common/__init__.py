"""Backend-neutral storage core shared by every adapter.

Chained JSONL codec, fault points, commit checks and the tx-log replay view.
The reference query evaluator lives in ``graph_cli.kernel.query_eval``.
Adapters (``memory``, ``files``) build on it and stay independent of each
other.
"""
