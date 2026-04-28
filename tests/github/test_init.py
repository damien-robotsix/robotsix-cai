import pytest
from unittest.mock import Mock, patch

from cai.github.init import _detect_origin, main

def test_detect_origin_valid_ssh():
    mock_origin = Mock()
    mock_origin.url = "git@github.com:owner/repo.git"
    
    mock_remotes = Mock()
    mock_remotes.origin = mock_origin
    
    mock_repo = Mock()
    mock_repo.remotes = mock_remotes
    
    with patch("cai.github.init.Repo", return_value=mock_repo):
        assert _detect_origin() == "owner/repo"

def test_detect_origin_valid_https():
    mock_origin = Mock()
    mock_origin.url = "https://github.com/owner/repo.git"
    
    mock_remotes = Mock()
    mock_remotes.origin = mock_origin
    
    mock_repo = Mock()
    mock_repo.remotes = mock_remotes
    
    with patch("cai.github.init.Repo", return_value=mock_repo):
        assert _detect_origin() == "owner/repo"

@patch("cai.github.init.set_local")
@patch("cai.github.init.unset_all_local")
@patch("cai.github.init.add_local")
@patch("cai.github.init._detect_origin", return_value="owner/repo")
@patch("cai.github.init.ensure_labels")
@patch("cai.github.init.CaiBot")
@patch("sys.argv", ["cai-app-init"])
def test_main_ensure_labels_called(mock_caibot_class, mock_detect_origin, mock_ensure_labels, mock_add_local, mock_unset_all_local, mock_set_local):
    mock_bot = Mock()
    mock_bot.app_id = "123456"
    mock_bot.installation_id.return_value = 111111
    mock_caibot_class.return_value = mock_bot
    
    main()
    
    mock_ensure_labels.assert_called_once()
    args, _ = mock_ensure_labels.call_args
    assert args[0] == mock_bot
    assert args[1] == "owner/repo"
    labels_passed = args[2]
    assert len(labels_passed) == 2
    assert labels_passed[0].name == "cai:raised"
    assert labels_passed[1].name == "cai:audit"
