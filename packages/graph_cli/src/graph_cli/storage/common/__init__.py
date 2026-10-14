"""Backend-neutral storage core shared by every adapter.

Chained JSONL codec, fault points, commit checks, the tx-log replay view and
the reference query evaluator. Adapters (``memory``, ``files``) build on it
and stay independent of each other.
"""
