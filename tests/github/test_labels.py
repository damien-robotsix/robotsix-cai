import pytest
from unittest.mock import Mock

from cai.github.labels import LabelSpec, ensure_labels


class TestLabelSpec:
    def test_creation_and_fields(self):
        spec = LabelSpec(name="cai:raised", color="0e8a16", description="Trigger cai")
        assert spec.name == "cai:raised"
        assert spec.color == "0e8a16"
        assert spec.description == "Trigger cai"

    def test_default_description_is_empty(self):
        spec = LabelSpec(name="test", color="ffffff")
        assert spec.description == ""

    def test_is_frozen(self):
        spec = LabelSpec(name="cai:raised", color="0e8a16")
        with pytest.raises(Exception):
            spec.name = "cai:audit"  # type: ignore[misc]

    def test_equality(self):
        a = LabelSpec(name="cai:raised", color="0e8a16", description="desc")
        b = LabelSpec(name="cai:raised", color="0e8a16", description="desc")
        c = LabelSpec(name="cai:raised", color="0e8a16", description="other")
        assert a == b
        assert a != c


class TestEnsureLabels:
    def test_creates_all_labels_when_none_exist(self):
        mock_bot = Mock()
        mock_repo = Mock()
        mock_bot.repo.return_value = mock_repo
        mock_repo.get_labels.return_value = []

        specs = [
            LabelSpec(name="cai:raised", color="0e8a16", description="Trigger cai"),
            LabelSpec(name="cai:audit", color="fbca04", description="For review"),
        ]

        result = ensure_labels(mock_bot, "owner/repo", specs)

        assert result == {"cai:raised": "created", "cai:audit": "created"}
        assert mock_repo.create_label.call_count == 2
        mock_repo.create_label.assert_any_call("cai:raised", "0e8a16", "Trigger cai")
        mock_repo.create_label.assert_any_call("cai:audit", "fbca04", "For review")

    def test_skips_existing_labels(self):
        mock_bot = Mock()
        mock_repo = Mock()
        mock_bot.repo.return_value = mock_repo

        existing_label = Mock()
        existing_label.name = "cai:raised"
        mock_repo.get_labels.return_value = [existing_label]

        specs = [
            LabelSpec(name="cai:raised", color="0e8a16", description="Trigger cai"),
            LabelSpec(name="cai:audit", color="fbca04", description="For review"),
        ]

        result = ensure_labels(mock_bot, "owner/repo", specs)

        assert result == {"cai:raised": "exists", "cai:audit": "created"}
        mock_repo.create_label.assert_called_once_with(
            "cai:audit", "fbca04", "For review"
        )

    def test_all_exist_returns_all_exists(self):
        mock_bot = Mock()
        mock_repo = Mock()
        mock_bot.repo.return_value = mock_repo

        label_a = Mock()
        label_a.name = "cai:raised"
        label_b = Mock()
        label_b.name = "cai:audit"
        mock_repo.get_labels.return_value = [label_a, label_b]

        specs = [
            LabelSpec(name="cai:raised", color="0e8a16"),
            LabelSpec(name="cai:audit", color="fbca04"),
        ]

        result = ensure_labels(mock_bot, "owner/repo", specs)

        assert result == {"cai:raised": "exists", "cai:audit": "exists"}
        mock_repo.create_label.assert_not_called()

    def test_preserves_existing_label_color(self):
        """ensure_labels must not overwrite existing labels — color changes are ignored."""
        mock_bot = Mock()
        mock_repo = Mock()
        mock_bot.repo.return_value = mock_repo

        existing = Mock()
        existing.name = "cai:raised"
        mock_repo.get_labels.return_value = [existing]

        specs = [
            LabelSpec(name="cai:raised", color="000000", description="won't apply"),
        ]

        result = ensure_labels(mock_bot, "owner/repo", specs)

        assert result == {"cai:raised": "exists"}
        mock_repo.create_label.assert_not_called()
