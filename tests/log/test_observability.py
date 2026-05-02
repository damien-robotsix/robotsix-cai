"""Tests for the langfuse_node_span context manager in cai.log.observability."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestLangfuseNodeSpan:
    """Tests for ``langfuse_node_span()`` — the per-node child-span context manager."""

    # ── no-op path ────────────────────────────────────────────────────────

    def test_noop_when_langfuse_not_configured(self):
        """When setup_langfuse() returns False, the context manager yields without creating a span."""
        with patch("cai.log.observability.setup_langfuse", return_value=False):
            with patch("langfuse.get_client") as mock_get_client:
                from cai.log.observability import langfuse_node_span

                with langfuse_node_span("test-span"):
                    pass  # should not raise

                mock_get_client.assert_not_called()

    def test_noop_does_not_import_langfuse_client(self):
        """When langfuse is not configured, no langfuse imports occur inside the body."""
        with patch("cai.log.observability.setup_langfuse", return_value=False):
            # If langfuse.get_client were called it would fail without the package
            with patch("langfuse.get_client") as mock_get_client:
                from cai.log.observability import langfuse_node_span

                with langfuse_node_span("any"):
                    yield_value = 42

                assert yield_value == 42
                mock_get_client.assert_not_called()

    # ── happy path ────────────────────────────────────────────────────────

    def test_creates_child_span_with_as_type_span(self):
        """When langfuse is configured, creates a child observation with as_type='span'."""
        mock_client = MagicMock()
        mock_observation_cm = MagicMock()
        mock_client.start_as_current_observation.return_value = mock_observation_cm

        with (
            patch("cai.log.observability.setup_langfuse", return_value=True),
            patch("langfuse.get_client", return_value=mock_client),
        ):
            from cai.log.observability import langfuse_node_span

            with langfuse_node_span("explore-span"):
                pass

        mock_client.start_as_current_observation.assert_called_once_with(
            name="explore-span",
            as_type="span",
            metadata=None,
        )

    def test_passes_metadata_to_span(self):
        """Metadata dict is forwarded to start_as_current_observation."""
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = MagicMock()

        metadata = {"phase": "research", "agent": "explore"}

        with (
            patch("cai.log.observability.setup_langfuse", return_value=True),
            patch("langfuse.get_client", return_value=mock_client),
        ):
            from cai.log.observability import langfuse_node_span

            with langfuse_node_span("explore", metadata=metadata):
                pass

        mock_client.start_as_current_observation.assert_called_once_with(
            name="explore",
            as_type="span",
            metadata=metadata,
        )

    def test_metadata_is_optional_defaults_to_none(self):
        """When metadata is not provided, it defaults to None."""
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = MagicMock()

        with (
            patch("cai.log.observability.setup_langfuse", return_value=True),
            patch("langfuse.get_client", return_value=mock_client),
        ):
            from cai.log.observability import langfuse_node_span

            with langfuse_node_span("no-meta"):
                pass

        mock_client.start_as_current_observation.assert_called_once_with(
            name="no-meta",
            as_type="span",
            metadata=None,
        )

    def test_empty_metadata_is_passed_through(self):
        """An empty metadata dict is forwarded as-is."""
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = MagicMock()

        with (
            patch("cai.log.observability.setup_langfuse", return_value=True),
            patch("langfuse.get_client", return_value=mock_client),
        ):
            from cai.log.observability import langfuse_node_span

            with langfuse_node_span("empty-meta", metadata={}):
                pass

        mock_client.start_as_current_observation.assert_called_once_with(
            name="empty-meta",
            as_type="span",
            metadata={},
        )

    # ── body execution ────────────────────────────────────────────────────

    def test_body_executes_within_span_context(self):
        """Code inside the with-block executes before the span exits."""
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = MagicMock()

        side_effects: list[str] = []

        with (
            patch("cai.log.observability.setup_langfuse", return_value=True),
            patch("langfuse.get_client", return_value=mock_client),
        ):
            from cai.log.observability import langfuse_node_span

            with langfuse_node_span("side-effects"):
                side_effects.append("inside")

        side_effects.append("outside")

        assert side_effects == ["inside", "outside"]

    def test_exception_in_body_propagates(self):
        """Exceptions raised inside the span body propagate normally."""
        mock_client = MagicMock()
        mock_client.start_as_current_observation.return_value = MagicMock()

        with (
            patch("cai.log.observability.setup_langfuse", return_value=True),
            patch("langfuse.get_client", return_value=mock_client),
        ):
            from cai.log.observability import langfuse_node_span

            with pytest.raises(RuntimeError, match="inside-span"):
                with langfuse_node_span("fail"):
                    raise RuntimeError("inside-span")

    # ── yield return value ────────────────────────────────────────────────

    def test_yields_none(self):
        """The context manager yields None (the body receives no value)."""
        with patch("cai.log.observability.setup_langfuse", return_value=False):
            from cai.log.observability import langfuse_node_span

            with langfuse_node_span("test") as yielded:
                assert yielded is None


class TestLangfuseNodeSpanExport:
    """Tests that langfuse_node_span is properly exported from cai.log."""

    def test_importable_from_cai_log(self):
        """langfuse_node_span is re-exported from cai.log."""
        from cai.log import langfuse_node_span

        assert callable(langfuse_node_span)

    def test_importable_from_cai_log_observability(self):
        """langfuse_node_span is defined in cai.log.observability."""
        from cai.log.observability import langfuse_node_span

        assert callable(langfuse_node_span)

    def test_same_function_referenced(self):
        """The cai.log export is the same function as the observability module's."""
        from cai.log import langfuse_node_span as exported
        from cai.log.observability import langfuse_node_span as defined

        assert exported is defined

    def test_included_in_all(self):
        """langfuse_node_span is listed in cai.log.__all__."""
        from cai.log import __all__

        assert "langfuse_node_span" in __all__


class TestLangfuseNodeSpanUsedInWorkflowNodes:
    """Integration-style tests verifying langfuse_node_span wraps agent calls.

    These verify that the import exists and the context manager is reachable
    from each workflow node module, without executing the actual agent calls.
    """

    def test_imported_in_explore_node(self):
        """langfuse_node_span is imported in cai.workflows.explore."""
        from cai.workflows.explore import langfuse_node_span as imported

        from cai.log.observability import langfuse_node_span as defined

        assert imported is defined

    def test_imported_in_refine_node(self):
        """langfuse_node_span is imported in cai.workflows.refine."""
        from cai.workflows.refine import langfuse_node_span as imported

        from cai.log.observability import langfuse_node_span as defined

        assert imported is defined

    def test_imported_in_implement_node(self):
        """langfuse_node_span is imported in cai.workflows.implement."""
        from cai.workflows.implement import langfuse_node_span as imported

        from cai.log.observability import langfuse_node_span as defined

        assert imported is defined
