"""Tests for cai.log.__init__ — the public API surface of the log package.

The :mod:`cai.log.__init__` module re-exports key functions from
:mod:`cai.log.observability` so that consumers can write
``from cai.log import setup_langfuse`` instead of reaching into the
inner module.  This test ensures the re-exports stay in sync with the
actual implementations.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

pytest.importorskip("genai_prices")


class TestPublicAPI:
    """Every name listed in ``__all__`` must be importable from ``cai.log``
    and must match the actual source function in ``cai.log.observability``.
    """

    # ── parametrised over every name in __all__ ───────────────────────────

    @pytest.mark.parametrize(
        "name",
        [
            "setup_langfuse",
            "langfuse_workflow",
            "session_id_for_pr",
            "traced_agent_run",
        ],
    )
    def test_every_all_name_is_importable(self, name: str) -> None:
        """Each ``__all__`` entry is importable from ``cai.log``."""
        mod = __import__("cai.log", fromlist=[name])
        public = getattr(mod, name, None)
        assert public is not None, f"cai.log.{name} is None"

    @pytest.mark.parametrize(
        "name",
        [
            "setup_langfuse",
            "langfuse_workflow",
            "session_id_for_pr",
            "traced_agent_run",
        ],
    )
    def test_re_export_matches_source(self, name: str) -> None:
        """The re-exported name is the *exact same object* as the one defined
        in ``cai.log.observability`` (identity, not equality)."""
        from cai.log import observability

        mod = __import__("cai.log", fromlist=[name])
        public = getattr(mod, name)
        source = getattr(observability, name)
        assert public is source, (
            f"cai.log.{name} is not cai.log.observability.{name}; "
            f"got {public!r}, expected {source!r}"
        )

    # ── __all__ completeness ──────────────────────────────────────────────

    def test_all_contains_exactly_four_names(self) -> None:
        """``__all__`` has exactly the four expected public names."""
        import cai.log  # noqa: PLC0415 — delayed import is fine here

        assert len(cai.log.__all__) == 4
        assert set(cai.log.__all__) == {
            "setup_langfuse",
            "langfuse_workflow",
            "session_id_for_pr",
            "traced_agent_run",
        }

    # ── negative: removed name should not exist ───────────────────────────

    def test_langfuse_node_span_does_not_exist(self) -> None:
        """The old, removed ``langfuse_node_span`` is **not** re-exported."""
        import cai.log  # noqa: PLC0415

        assert not hasattr(cai.log, "langfuse_node_span")

    def test_langfuse_node_span_not_in_all(self) -> None:
        """``__all__`` does **not** include the removed name."""
        import cai.log  # noqa: PLC0415

        assert "langfuse_node_span" not in cai.log.__all__

    # ── import without external env vars ──────────────────────────────────

    def test_import_does_not_require_langfuse_env(self) -> None:
        """Importing ``cai.log`` should succeed when Langfuse env vars are absent
        (the observability module only touches Langfuse when a function is called)."""
        # Remove only the Langfuse-related env vars; keep everything else.
        langfuse_vars = {"LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
                         "LANGFUSE_TIMEOUT"}
        with patch.dict("os.environ", {k: "" for k in langfuse_vars}, clear=False):
            import importlib  # noqa: PLC0415
            import os

            # Unload cached modules so the re-import exercises the fresh path
            for mod in ("cai.log.observability", "cai.log"):
                if mod in sys.modules:
                    del sys.modules[mod]

            # Force os.environ lookups to see the patched empty values
            import cai.log  # noqa: PLC0415, F811, F401

            importlib.reload(cai.log)
            # If we reach here the import did not raise
            assert not os.environ.get("LANGFUSE_PUBLIC_KEY")
