"""Kronos CLI entrypoints."""
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    # Side-effect import: pull eventlet in early (oslo.messaging picks
    # it up at import time) with its deprecation warnings silenced.
    import eventlet  # noqa: F401  # pyright: ignore[reportUnusedImport]
