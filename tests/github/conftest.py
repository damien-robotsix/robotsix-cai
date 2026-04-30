from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def push_mocks() -> Iterator[tuple[MagicMock, MagicMock, MagicMock]]:
    with (
        patch("cai.github.issues.CaiBot") as mock_caibot_class,
        patch("cai.github.issues.ensure_labels") as mock_ensure_labels,
        patch("cai.github.issues._resolve_milestone") as mock_resolve_milestone,
    ):
        yield mock_caibot_class, mock_ensure_labels, mock_resolve_milestone
