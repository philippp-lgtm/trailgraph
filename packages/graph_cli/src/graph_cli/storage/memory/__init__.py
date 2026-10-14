"""InMemoryStore: the reference backend (contracts B13, section 6.32).

Proves together with FileStore that the storage port is not file-shaped, and
gives fast tests. It shares ``storage.common`` with FileStore but never
imports ``storage.files``.
"""
