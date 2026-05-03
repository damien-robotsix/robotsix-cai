from __future__ import annotations

from pathlib import Path

import pytest

from cai.workflows.state import (
    DocsOutput,
    ImplementOutput,
    SessionState,
    ThreadReply,
    load_session_state,
    save_session_state,
)


# ---------------------------------------------------------------------------
# ImplementOutput — files_changed field
# ---------------------------------------------------------------------------


def test_implement_output_files_changed_defaults_to_empty_list():
    """files_changed defaults to an empty list when not provided."""
    output = ImplementOutput(
        summary="Implemented feature X.",
        commit_message="feat: implement feature X",
    )
    assert output.files_changed == []


def test_implement_output_files_changed_accepts_list_of_strings():
    """files_changed accepts and stores a list of repo-relative paths."""
    files = ["src/a.py", "src/b.py", "tests/test_a.py"]
    output = ImplementOutput(
        summary="Implemented feature X.",
        commit_message="feat: implement feature X",
        files_changed=files,
    )
    assert output.files_changed == files


def test_implement_output_files_changed_preserves_order():
    """files_changed preserves the order of paths as provided."""
    files = ["z.py", "a.py", "m.py"]
    output = ImplementOutput(
        summary="Implemented feature X.",
        commit_message="feat: implement feature X",
        files_changed=files,
    )
    assert output.files_changed == ["z.py", "a.py", "m.py"]


def test_implement_output_json_schema_includes_files_changed():
    """The JSON schema for ImplementOutput includes the files_changed field."""
    schema = ImplementOutput.model_json_schema()
    props = schema.get("properties", {})
    assert "files_changed" in props, (
        "files_changed must appear in the JSON schema properties"
    )
    assert props["files_changed"].get("title") == "Files Changed"
    assert props["files_changed"].get("type") == "array"


def test_implement_output_existing_fields_still_work():
    """Adding files_changed does not break existing fields like summary,
    commit_message, required_checks, or replies."""
    replies = [
        ThreadReply(
            thread_id="thread_1",
            action="fix",
            reply="Fixed the import issue.",
        ),
    ]
    output = ImplementOutput(
        summary="Fixed import issue.",
        commit_message="fix: resolve circular import",
        required_checks=["python"],
        replies=replies,
        files_changed=["src/module.py"],
    )
    assert output.summary == "Fixed import issue."
    assert output.commit_message == "fix: resolve circular import"
    assert output.required_checks == ["python"]
    assert len(output.replies) == 1
    assert output.replies[0].thread_id == "thread_1"
    assert output.files_changed == ["src/module.py"]


# ---------------------------------------------------------------------------
# DocsOutput — files_changed field
# ---------------------------------------------------------------------------


def test_docs_output_files_changed_defaults_to_empty_list():
    """files_changed defaults to an empty list when not provided."""
    output = DocsOutput(
        summary="Updated docs/cli.md to cover the new flag.",
        commit_message="docs: document --timeout flag in cli.md",
    )
    assert output.files_changed == []


def test_docs_output_files_changed_accepts_list_of_strings():
    """files_changed accepts and stores a list of repo-relative paths."""
    files = ["docs/cli.md", "docs/index.md"]
    output = DocsOutput(
        summary="Updated CLI and index docs.",
        commit_message="docs: update cli and index pages",
        files_changed=files,
    )
    assert output.files_changed == files


def test_docs_output_files_changed_preserves_order():
    """files_changed preserves the order of paths as provided."""
    files = ["docs/z.md", "docs/a.md", "docs/m.md"]
    output = DocsOutput(
        summary="Updated multiple docs pages.",
        commit_message="docs: update multiple pages",
        files_changed=files,
    )
    assert output.files_changed == ["docs/z.md", "docs/a.md", "docs/m.md"]


def test_docs_output_json_schema_includes_files_changed():
    """The JSON schema for DocsOutput includes the files_changed field."""
    schema = DocsOutput.model_json_schema()
    props = schema.get("properties", {})
    assert "files_changed" in props, (
        "files_changed must appear in the JSON schema properties"
    )
    assert props["files_changed"].get("title") == "Files Changed"
    assert props["files_changed"].get("type") == "array"


def test_docs_output_existing_fields_still_work():
    """Adding files_changed does not break existing fields like summary or
    commit_message."""
    output = DocsOutput(
        summary="Updated docs/cli.md to cover the new --timeout flag.",
        commit_message="docs: document --timeout flag in cli.md",
        files_changed=["docs/cli.md"],
    )
    assert output.summary == (
        "Updated docs/cli.md to cover the new --timeout flag."
    )
    assert output.commit_message == "docs: document --timeout flag in cli.md"
    assert output.files_changed == ["docs/cli.md"]


# ---------------------------------------------------------------------------
# SessionState model
# ---------------------------------------------------------------------------


class TestSessionState:
    def test_defaults(self):
        """SessionState fields have sensible defaults."""
        state = SessionState()
        assert state.explore_findings == ""
        assert state.explore_files == []
        assert state.known_corruptions == []
        assert state.attempt_count == 0
        assert state.prior_file_hashes == {}

    def test_explicit_values(self):
        """SessionState accepts all fields via constructor."""
        state = SessionState(
            explore_findings="Found the authentication module.",
            explore_files=["src/auth.py", "src/config.py"],
            known_corruptions=["test_refine.py was corrupted in a prior run"],
            attempt_count=3,
            prior_file_hashes={"src/auth.py": "abc123"},
        )
        assert state.explore_findings == "Found the authentication module."
        assert state.explore_files == ["src/auth.py", "src/config.py"]
        assert state.known_corruptions == ["test_refine.py was corrupted in a prior run"]
        assert state.attempt_count == 3
        assert state.prior_file_hashes == {"src/auth.py": "abc123"}

    def test_attempt_count_increments(self):
        """attempt_count is a plain int that can be incremented externally."""
        state = SessionState(attempt_count=1)
        state.attempt_count += 1
        assert state.attempt_count == 2

    def test_json_round_trip(self):
        """SessionState serialises and deserialises without data loss."""
        original = SessionState(
            explore_findings="Found the bug.",
            explore_files=["src/bug.py"],
            known_corruptions=["corrupt_file.py"],
            attempt_count=2,
            prior_file_hashes={"src/bug.py": "def456"},
        )
        json_str = original.model_dump_json(indent=2)
        restored = SessionState.model_validate_json(json_str)
        assert restored.explore_findings == original.explore_findings
        assert restored.explore_files == original.explore_files
        assert restored.known_corruptions == original.known_corruptions
        assert restored.attempt_count == original.attempt_count
        assert restored.prior_file_hashes == original.prior_file_hashes


# ---------------------------------------------------------------------------
# load_session_state / save_session_state
# ---------------------------------------------------------------------------


def test_load_session_state_file_missing(tmp_path: Path):
    """load_session_state returns a default SessionState when no file exists."""
    state = load_session_state(tmp_path)
    assert isinstance(state, SessionState)
    assert state.attempt_count == 0
    assert state.explore_findings == ""


def test_load_session_state_file_exists(tmp_path: Path):
    """load_session_state reads and parses an existing session_state.json."""
    state_file = tmp_path / "session_state.json"
    state_file.write_text(
        '{\n'
        '  "explore_findings": "Found auth module.",\n'
        '  "explore_files": ["src/auth.py"],\n'
        '  "known_corruptions": [],\n'
        '  "attempt_count": 2,\n'
        '  "prior_file_hashes": {}\n'
        '}'
    )
    state = load_session_state(tmp_path)
    assert state.explore_findings == "Found auth module."
    assert state.explore_files == ["src/auth.py"]
    assert state.attempt_count == 2


def test_save_session_state_writes_file(tmp_path: Path):
    """save_session_state writes SessionState as JSON to session_state.json."""
    state = SessionState(
        explore_findings="Results.",
        explore_files=["src/x.py"],
        attempt_count=1,
    )
    save_session_state(state, tmp_path)
    state_file = tmp_path / "session_state.json"
    assert state_file.exists()
    content = state_file.read_text()
    assert "explore_findings" in content
    assert "Results." in content
    assert "src/x.py" in content
    assert '"attempt_count": 1' in content


def test_save_session_state_round_trip(tmp_path: Path):
    """State saved and then loaded preserves all fields."""
    original = SessionState(
        explore_findings="Round trip test.",
        explore_files=["a.py", "b.py"],
        known_corruptions=["c.py"],
        attempt_count=5,
        prior_file_hashes={"a.py": "hash1"},
    )
    save_session_state(original, tmp_path)
    restored = load_session_state(tmp_path)
    assert restored.explore_findings == original.explore_findings
    assert restored.explore_files == original.explore_files
    assert restored.known_corruptions == original.known_corruptions
    assert restored.attempt_count == original.attempt_count
    assert restored.prior_file_hashes == original.prior_file_hashes


def test_load_session_state_corrupt_file(tmp_path: Path):
    """load_session_state raises when the JSON file is malformed."""
    state_file = tmp_path / "session_state.json"
    state_file.write_text("not valid json")
    with pytest.raises(Exception):
        load_session_state(tmp_path)
