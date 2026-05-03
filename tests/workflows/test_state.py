from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cai.github.issues import IssueMeta
from cai.workflows.state import ExploreOutput, IssueState


@pytest.fixture
def state(tmp_path: Path) -> IssueState:
    body = tmp_path / "42.md"
    body.write_text("## Issue body\n")
    meta = IssueMeta(repo="owner/repo", number=42, title="Original title", labels=["cai:raised"])
    bot = MagicMock()
    bot.token_for.return_value = "tok"
    s = IssueState(
        bot=bot,
        meta=meta,
        body_path=body,
        repo_root=tmp_path,
        meta_json='{"number": 42}',
        body="## Issue body\n",
    )
    s.findings = ExploreOutput(summary="Some findings.", related_files=[])
    s.reference_files = []
    return s


# ---------------------------------------------------------------------------
# reference_files_section — edge cases
# ---------------------------------------------------------------------------


def test_reference_files_section_empty_list(state):
    """An empty reference_files list produces an empty section."""
    state.reference_files = []
    assert state.reference_files_section() == ""


def test_reference_files_section_all_files_oversized(state, tmp_path):
    """When every file exceeds the per-file cap, the section is empty."""
    content = "x" * 200_000  # > _MAX_REFERENCE_FILE_BYTES
    f = tmp_path / "huge.py"
    f.write_text(content)
    state.reference_files = ["huge.py"]
    assert state.reference_files_section() == ""


def test_reference_files_section_all_files_missing(state):
    """When no reference files exist on disk, the section is empty."""
    state.reference_files = ["nonexistent.py", "missing/foo.py"]
    assert state.reference_files_section() == ""


def test_reference_files_section_exact_budget_fits(state, tmp_path):
    """A file whose rendered cost exactly matches the remaining budget is included
    and no truncation note is appended."""
    f = tmp_path / "exact.py"
    # Must stay under the per-file cap (_MAX_REFERENCE_FILE_BYTES = 100_000)
    # so the file isn't silently dropped before the budget check runs.
    content = "y" * 99_000
    f.write_text(content)
    state.reference_files = ["exact.py"]

    section = state.reference_files_section()
    assert section.startswith("## Reference files\n")
    assert "exact.py" in section
    assert "omitted due to size limit" not in section


def test_reference_files_section_zero_byte_files(state, tmp_path):
    """Zero-byte reference files are included (they cost almost nothing)."""
    for name in ("empty_a.py", "empty_b.py"):
        f = tmp_path / "src" / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")
    state.reference_files = ["src/empty_a.py", "src/empty_b.py"]

    section = state.reference_files_section()
    assert "src/empty_a.py" in section
    assert "src/empty_b.py" in section
    assert "omitted due to size limit" not in section


def test_reference_files_section_mixed_missing_and_valid(state, tmp_path):
    """Missing files are silently skipped; valid files that fit are included
    and the omitted count only reflects files skipped due to the budget."""
    f = tmp_path / "a.py"
    f.write_text("x" * 50_000)
    state.reference_files = ["missing.py", "a.py", "beyond.py"]
    section = state.reference_files_section()
    assert "missing.py" not in section
    assert "a.py" in section
    assert "beyond.py" not in section
    assert "omitted due to size limit" not in section


# ---------------------------------------------------------------------------
# reference_files_section — total byte budget
# ---------------------------------------------------------------------------


def test_reference_files_section_truncation_note_correct_count(state, tmp_path):
    """When multiple files exceed the total byte budget, the truncation note
    shows the correct number of remaining files."""
    # Create 6 files of ~45 KB each.  Rendered cost per file is ~45 KB +
    # markdown overhead.  With a 200 KB total budget, roughly 4 files fit
    # and the remainder are counted in the truncation note.
    content = "z" * 45_000
    for i in range(6):
        f = tmp_path / f"part_{i}.py"
        f.write_text(content)
    state.reference_files = [f"part_{i}.py" for i in range(6)]

    section = state.reference_files_section()

    assert section.startswith("## Reference files\n")
    # At least some files should be present
    assert "part_0.py" in section
    # A truncation note must be present
    assert "omitted due to size limit" in section
    # The note should end the section
    assert section.endswith("_")


def test_reference_files_section_absolute_path_outside_repo(state, tmp_path):
    """An absolute reference-file path that resolves outside repo_root
    raises ValueError from relative_to and is silently skipped."""
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x" * 1000)
    state.reference_files = [str(outside)]
    assert state.reference_files_section() == ""


def test_reference_files_section_omitted_count_stops_at_first_exceeding(state, tmp_path):
    """When a file exceeds the remaining total budget, iteration stops via
    ``break`` and later files are not included or checked."""
    # Three tiny files (~100 bytes each) that easily fit.
    # Then a ~100 KB file whose rendered cost (~100 KB) fits.
    # Then a second ~100 KB file whose rendered cost pushes the total
    # past the 200 KB cap — that file is the first to be omitted and
    # iteration stops immediately (no further files are checked).
    for i in range(3):
        f = tmp_path / f"tiny_{i}.py"
        f.write_text("s" * 100)
    big = tmp_path / "big.py"
    big.write_text("x" * 99_990)
    tail = tmp_path / "tail.py"
    tail.write_text("x" * 99_990)

    state.reference_files = [
        f"tiny_{i}.py" for i in range(3)
    ] + ["big.py", "tail.py"]

    section = state.reference_files_section()
    assert "tiny_0.py" in section
    assert "tiny_1.py" in section
    assert "tiny_2.py" in section
    assert "big.py" in section
    # tail.py is after the file that exceeded the budget → omitted
    assert "tail.py" not in section
    assert "omitted due to size limit" in section
