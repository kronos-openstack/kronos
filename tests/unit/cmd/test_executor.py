"""Tests for the kronos-executor CLI scope resolution.

``resolve_scopes`` turns the ``--aggregate`` (repeatable) and
``--unassigned`` options into the ordered list of scopes the worker
services.  ``None`` in the result represents the unassigned-hosts pool.
"""

from __future__ import annotations

import pytest

from kronos.cmd.executor import resolve_scopes


class TestResolveScopes:
    def test_single_aggregate(self) -> None:
        assert resolve_scopes(["gpu"], unassigned=False) == ["gpu"]

    def test_unassigned_only(self) -> None:
        assert resolve_scopes([], unassigned=True) == [None]

    def test_multiple_aggregates_preserve_order(self) -> None:
        assert resolve_scopes(["gpu", "hpc", "cpu"], unassigned=False) == [
            "gpu",
            "hpc",
            "cpu",
        ]

    def test_aggregates_plus_unassigned(self) -> None:
        """Named aggregates first, unassigned pool (None) last."""
        assert resolve_scopes(["gpu", "hpc"], unassigned=True) == [
            "gpu",
            "hpc",
            None,
        ]

    def test_duplicate_aggregates_dropped(self) -> None:
        """A repeated name is collapsed so we don't start two consumers."""
        assert resolve_scopes(["gpu", "hpc", "gpu"], unassigned=False) == [
            "gpu",
            "hpc",
        ]

    def test_no_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            resolve_scopes([], unassigned=False)
