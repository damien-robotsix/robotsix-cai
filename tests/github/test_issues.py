import json
from pathlib import Path
import pytest
from unittest.mock import Mock, patch

from cai.github.issues import push, _resolve_milestone

@patch("cai.github.issues._resolve_milestone")
@patch("cai.github.issues.ensure_labels")
@patch("cai.github.issues.CaiBot")
def test_push_ensure_labels_called_for_cai_raised(mock_caibot_class, mock_ensure_labels, mock_resolve_milestone, tmp_path):
    mock_bot = Mock()
    mock_caibot_class.return_value = mock_bot
    mock_repo = Mock()
    mock_bot.repo.return_value = mock_repo
    mock_issue = Mock()
    mock_issue.number = 42
    mock_repo.create_issue.return_value = mock_issue
    
    json_path = tmp_path / "new_issue.json"
    json_path.write_text(json.dumps({
        "repo": "owner/repo",
        "title": "Test Issue",
        "labels": ["cai:raised"]
    }))
    
    md_path = tmp_path / "new_issue.md"
    md_path.write_text("Test Body")
    
    push(mock_bot, json_path)
    
    mock_ensure_labels.assert_called_once()
    args, _ = mock_ensure_labels.call_args
    assert args[0] == mock_bot
    assert args[1] == "owner/repo"
    labels_passed = args[2]
    assert len(labels_passed) == 2
    assert labels_passed[0].name == "cai:raised"
    assert labels_passed[1].name == "cai:audit"

@patch("cai.github.issues._resolve_milestone")
@patch("cai.github.issues.ensure_labels")
@patch("cai.github.issues.CaiBot")
def test_push_ensure_labels_called_for_cai_audit(mock_caibot_class, mock_ensure_labels, mock_resolve_milestone, tmp_path):
    mock_bot = Mock()
    mock_caibot_class.return_value = mock_bot
    mock_repo = Mock()
    mock_bot.repo.return_value = mock_repo
    
    mock_issue = Mock()
    mock_issue.number = 42
    mock_repo.create_issue.return_value = mock_issue
    
    json_path = tmp_path / "new_issue.json"
    json_path.write_text(json.dumps({
        "repo": "owner/repo",
        "title": "Test Issue",
        "labels": ["cai:audit"]
    }))
    
    md_path = tmp_path / "new_issue.md"
    md_path.write_text("Test Body")
    
    push(mock_bot, json_path)
    
    mock_ensure_labels.assert_called_once()
    args, _ = mock_ensure_labels.call_args
    assert args[0] == mock_bot
    assert args[1] == "owner/repo"

@patch("cai.github.issues._resolve_milestone")
@patch("cai.github.issues.ensure_labels")
@patch("cai.github.issues.CaiBot")
def test_push_ensure_labels_not_called_for_other_labels(mock_caibot_class, mock_ensure_labels, mock_resolve_milestone, tmp_path):
    mock_bot = Mock()
    mock_caibot_class.return_value = mock_bot
    mock_repo = Mock()
    mock_bot.repo.return_value = mock_repo
    
    mock_issue = Mock()
    mock_issue.number = 42
    mock_repo.create_issue.return_value = mock_issue
    
    json_path = tmp_path / "new_issue.json"
    json_path.write_text(json.dumps({
        "repo": "owner/repo",
        "title": "Test Issue",
        "labels": ["enhancement", "bug"]
    }))
    
    md_path = tmp_path / "new_issue.md"
    md_path.write_text("Test Body")
    
    push(mock_bot, json_path)
    
    mock_ensure_labels.assert_not_called()
