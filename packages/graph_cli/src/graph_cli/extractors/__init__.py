"""Versioned extractors of type-specific file metadata (file metadata plan, stage 1).

Pure and domain-neutral: byte parsers and a child-process runner, no services, no storage.
An extractor is a deterministic function of the file's bytes (and the media type the file was
recorded with); its ``version`` is part of the cache key of its result. See ``model`` for the
contract, ``registry`` for the static list and ``runner`` for the isolated run.
"""
