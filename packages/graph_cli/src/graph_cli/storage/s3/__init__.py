"""S3-compatible object store client for stored file bytes (object storage stage 1, O2).

Standard library only (Decision 3): SigV4 signing over ``http.client`` and ``ssl``.
Imports only the kernel and the standard library; never reads the environment
(``catalog`` builds the ``ObjectStoreSettings`` and hands them in, O1).
"""
