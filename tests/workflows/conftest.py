from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cai.github.issues import IssueMeta
from cai.workflows.state import IssueState


@pytest.fixture
def state(tmp_path: Path) -> IssueState:
    body = tmp_path / "body.md"
    body.write_text("body")
    meta = IssueMeta(repo="o/r", number=99, title="t")
    bot = MagicMock()
    bot.token_for.return_value = "tok"
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        branch_name="feature/x",
    )
    s.new_meta = meta
    return s
