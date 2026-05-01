"""Kronos CLI entrypoints."""
import warnings

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    import eventlet  # noqa: F401
