import re
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import pytest

from cai.github.labels import CAI_LABEL_SPECS, LabelSpec, ensure_labels


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
        with pytest.raises(FrozenInstanceError):
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


class TestCAILabelSpecs:
    """Tests for the shared CAI_LABEL_SPECS constant."""

    # Expected values for each label, in declared order
    EXPECTED = [
        ("cai:raised", "0e8a16", "Trigger cai to solve"),
        ("cai:audit", "fbca04", "For cai to review"),
        ("cai:pr-ready", "0e8a16", "CAI solve completed; PR opened"),
        ("cai:failed", "b60205", "CAI solve did not complete"),
        ("cai:human-review", "1d76db", "Awaiting human review/merge — CAI is done"),
        ("cai:sub-issue", "bfdadc", "Sub-issue of a parent issue — tracked for parent completion checks"),
        ("cai:trace-investigation", "d93f0b", "Symptom seen in agent traces — confirm by inspecting the listed traces before acting"),
    ]

    def test_is_list_of_seven(self):
        assert isinstance(CAI_LABEL_SPECS, list)
        assert len(CAI_LABEL_SPECS) == 7

    def test_all_items_are_label_specs(self):
        for spec in CAI_LABEL_SPECS:
            assert isinstance(spec, LabelSpec)

    @pytest.mark.parametrize("index", range(7))
    def test_each_spec_has_correct_fields(self, index):
        spec = CAI_LABEL_SPECS[index]
        expected_name, expected_color, expected_desc = self.EXPECTED[index]
        assert spec.name == expected_name
        assert spec.color == expected_color
        assert spec.description == expected_desc

    def test_no_duplicate_names(self):
        names = [spec.name for spec in CAI_LABEL_SPECS]
        assert len(names) == len(set(names))

    def test_all_names_have_cai_prefix(self):
        for spec in CAI_LABEL_SPECS:
            assert spec.name.startswith("cai:"), f"{spec.name} should start with 'cai:'"

    def test_all_colors_are_six_char_hex(self):
        hex_color = re.compile(r"^[0-9a-fA-F]{6}$")
        for spec in CAI_LABEL_SPECS:
            assert hex_color.match(spec.color), f"{spec.color} is not 6-char hex"

    def test_all_descriptions_are_non_empty(self):
        for spec in CAI_LABEL_SPECS:
            assert spec.description, f"{spec.name} should have a non-empty description"

    def test_re_exported_from_cai_github(self):
        """CAI_LABEL_SPECS imported from cai.github is the same object."""
        from cai.github import CAI_LABEL_SPECS as re_exported
        assert re_exported is CAI_LABEL_SPECS

    def test_passed_to_ensure_labels_creates_all(self):
        """The constant integrates with ensure_labels: all labels are created."""
        mock_bot = Mock()
        mock_repo = Mock()
        mock_bot.repo.return_value = mock_repo
        mock_repo.get_labels.return_value = []

        result = ensure_labels(mock_bot, "owner/repo", CAI_LABEL_SPECS)

        assert result == {spec.name: "created" for spec in CAI_LABEL_SPECS}
        assert mock_repo.create_label.call_count == 7
        for spec in CAI_LABEL_SPECS:
            mock_repo.create_label.assert_any_call(spec.name, spec.color, spec.description)
