"""Extraction engines: bytes → normalized text/records, one module per family.

Each engine lazily imports its heavy third-party dependency so the service still
boots (and other content types still work) when an optional lib is absent — a
missing dep degrades that one content type, it does not crash the process.
"""
