"""Tests for cai.github.pr — review threads, parsing, and GraphQL helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from cai.github.pr import (
    ReviewComment,
    ReviewThread,
    _get_review_threads_nodes,
    _graphql,
    _parse_thread_node,
    list_resolved_threads,
    list_unresolved_threads,
    resolve_review_thread,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bot() -> MagicMock:
    """A CaiBot mock that returns a fake token."""
    b = MagicMock()
    b.token_for.return_value = "fake-token"
    return b


# ---------------------------------------------------------------------------
# ReviewComment
# ---------------------------------------------------------------------------


def test_review_comment_creation():
    c = ReviewComment(author="alice", body="LGTM", created_at="2025-01-01T00:00:00Z")
    assert c.author == "alice"
    assert c.body == "LGTM"
    assert c.created_at == "2025-01-01T00:00:00Z"


def test_review_comment_frozen():
    c = ReviewComment(author="alice", body="LGTM", created_at="2025-01-01T00:00:00Z")
    with pytest.raises(Exception):
        c.author = "bob"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ReviewThread
# ---------------------------------------------------------------------------


def test_review_thread_creation():
    comments = [
        ReviewComment(author="alice", body="Please fix", created_at="2025-01-01T00:00:00Z"),
        ReviewComment(author="bob", body="Done", created_at="2025-01-02T00:00:00Z"),
    ]
    thread = ReviewThread(
        id="node-123",
        path="src/foo.py",
        line=42,
        diff_hunk="@@ -40,3 +40,7 @@",
        first_comment_id=1001,
        comments=comments,
    )
    assert thread.id == "node-123"
    assert thread.path == "src/foo.py"
    assert thread.line == 42
    assert thread.diff_hunk == "@@ -40,3 +40,7 @@"
    assert thread.first_comment_id == 1001
    assert len(thread.comments) == 2


def test_review_thread_line_none():
    thread = ReviewThread(
        id="node-123",
        path="README.md",
        line=None,
        diff_hunk="",
        first_comment_id=1001,
        comments=[],
    )
    assert thread.line is None


def test_review_thread_frozen():
    thread = ReviewThread(
        id="node-123",
        path="src/foo.py",
        line=42,
        diff_hunk="",
        first_comment_id=1001,
        comments=[],
    )
    with pytest.raises(Exception):
        thread.path = "other.py"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _parse_thread_node
# ---------------------------------------------------------------------------

_HEAD_COMMENT_NODE = {
    "databaseId": 1001,
    "author": {"login": "alice"},
    "body": "Please fix this",
    "diffHunk": "@@ -1,1 +1,1 @@",
    "createdAt": "2025-01-01T00:00:00Z",
}

_REPLY_NODE = {
    "databaseId": 1002,
    "author": {"login": "bob"},
    "body": "Done",
    "diffHunk": None,
    "createdAt": "2025-01-02T00:00:00Z",
}

_BASE_THREAD_NODE = {
    "id": "node-abc",
    "path": "src/foo.py",
    "line": 42,
    "comments": {"nodes": [_HEAD_COMMENT_NODE, _REPLY_NODE]},
}


def test_parse_thread_node_basic():
    result = _parse_thread_node(_BASE_THREAD_NODE)
    assert result is not None
    assert result.id == "node-abc"
    assert result.path == "src/foo.py"
    assert result.line == 42
    assert result.diff_hunk == "@@ -1,1 +1,1 @@"
    assert result.first_comment_id == 1001
    assert len(result.comments) == 2

    head, reply = result.comments
    assert head.author == "alice"
    assert head.body == "Please fix this"
    assert reply.author == "bob"
    assert reply.body == "Done"


def test_parse_thread_node_empty_comments_returns_none():
    node = {
        "id": "node-abc",
        "path": "src/foo.py",
        "line": 42,
        "comments": {"nodes": []},
    }
    assert _parse_thread_node(node) is None


def test_parse_thread_node_ghost_author():
    """When author is None or absent, login should fall back to 'ghost'."""
    head_no_author = dict(_HEAD_COMMENT_NODE)
    head_no_author["author"] = None
    node = {
        "id": "node-abc",
        "path": "src/foo.py",
        "line": None,
        "comments": {"nodes": [head_no_author]},
    }
    result = _parse_thread_node(node)
    assert result is not None
    assert result.comments[0].author == "ghost"


def test_parse_thread_node_missing_author_field():
    """When author key is missing entirely, login should fall back to 'ghost'."""
    head_no_author = dict(_HEAD_COMMENT_NODE)
    del head_no_author["author"]
    node = {
        "id": "node-abc",
        "path": "src/foo.py",
        "line": None,
        "comments": {"nodes": [head_no_author]},
    }
    result = _parse_thread_node(node)
    assert result is not None
    assert result.comments[0].author == "ghost"


def test_parse_thread_node_empty_author_dict():
    """When author is an empty dict, get('login') returns None → 'ghost'."""
    head_empty_author = dict(_HEAD_COMMENT_NODE)
    head_empty_author["author"] = {}
    node = {
        "id": "node-abc",
        "path": "src/foo.py",
        "line": None,
        "comments": {"nodes": [head_empty_author]},
    }
    result = _parse_thread_node(node)
    assert result is not None
    assert result.comments[0].author == "ghost"


def test_parse_thread_node_null_diff_hunk():
    """When diffHunk is None, it falls back to ''."""
    head_null_hunk = dict(_HEAD_COMMENT_NODE)
    head_null_hunk["diffHunk"] = None
    node = {
        "id": "node-abc",
        "path": "src/foo.py",
        "line": None,
        "comments": {"nodes": [head_null_hunk]},
    }
    result = _parse_thread_node(node)
    assert result is not None
    assert result.diff_hunk == ""


def test_parse_thread_node_line_none():
    node = {
        "id": "node-abc",
        "path": "src/foo.py",
        "line": None,
        "comments": {"nodes": [_HEAD_COMMENT_NODE]},
    }
    result = _parse_thread_node(node)
    assert result is not None
    assert result.line is None


# ---------------------------------------------------------------------------
# _get_review_threads_nodes
# ---------------------------------------------------------------------------


def _make_graphql_response(nodes):
    """Build the nested dict that _graphql would return."""
    return {
        "repository": {
            "pullRequest": {
                "reviewThreads": {"nodes": nodes},
            }
        }
    }


@patch("cai.github.pr._graphql")
def test_get_review_threads_nodes_returns_nodes(mock_graphql, bot):
    nodes = [{"id": "n1"}, {"id": "n2"}]
    mock_graphql.return_value = _make_graphql_response(nodes)

    result = _get_review_threads_nodes(bot, "owner/name", 42)

    assert result == nodes
    mock_graphql.assert_called_once()
    # Verify the variables passed to _graphql
    _, _, variables = mock_graphql.call_args[0]
    assert variables["owner"] == "owner"
    assert variables["name"] == "name"
    assert variables["number"] == 42


@patch("cai.github.pr._graphql")
def test_get_review_threads_nodes_empty(mock_graphql, bot):
    mock_graphql.return_value = _make_graphql_response([])

    result = _get_review_threads_nodes(bot, "owner/name", 7)

    assert result == []


# ---------------------------------------------------------------------------
# list_unresolved_threads
# ---------------------------------------------------------------------------

_UNRESOLVED_NODE = {
    "id": "n-unresolved",
    "isResolved": False,
    "isOutdated": False,
    "path": "src/foo.py",
    "line": 10,
    "comments": {
        "nodes": [
            {
                "databaseId": 1,
                "author": {"login": "human"},
                "body": "nit: rename",
                "diffHunk": "@@ -9,1 +9,1 @@",
                "createdAt": "2025-01-01T00:00:00Z",
            }
        ]
    },
}

_RESOLVED_NODE = {
    "id": "n-resolved",
    "isResolved": True,
    "isOutdated": False,
    "path": "src/bar.py",
    "line": 20,
    "comments": {
        "nodes": [
            {
                "databaseId": 2,
                "author": {"login": "human"},
                "body": "typo",
                "diffHunk": "@@ -19,1 +19,1 @@",
                "createdAt": "2025-01-02T00:00:00Z",
            }
        ]
    },
}

_OUTDATED_NODE = {
    "id": "n-outdated",
    "isResolved": False,
    "isOutdated": True,
    "path": "src/baz.py",
    "line": 30,
    "comments": {
        "nodes": [
            {
                "databaseId": 3,
                "author": {"login": "human"},
                "body": "old stuff",
                "diffHunk": "@@ -29,1 +29,1 @@",
                "createdAt": "2025-01-03T00:00:00Z",
            }
        ]
    },
}

_BOT_THREAD_NODE = {
    "id": "n-bot",
    "isResolved": False,
    "isOutdated": False,
    "path": "src/qux.py",
    "line": 40,
    "comments": {
        "nodes": [
            {
                "databaseId": 4,
                "author": {"login": "cai[bot]"},
                "body": "automated comment",
                "diffHunk": "@@ -39,1 +39,1 @@",
                "createdAt": "2025-01-04T00:00:00Z",
            }
        ]
    },
}

_EMPTY_COMMENTS_NODE = {
    "id": "n-empty",
    "isResolved": False,
    "isOutdated": False,
    "path": "src/empty.py",
    "line": 50,
    "comments": {"nodes": []},
}


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_only_unresolved(mock_nodes, bot):
    mock_nodes.return_value = [_UNRESOLVED_NODE, _RESOLVED_NODE, _OUTDATED_NODE]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 1
    assert threads[0].id == "n-unresolved"


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_skips_resolved(mock_nodes, bot):
    mock_nodes.return_value = [_RESOLVED_NODE]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_skips_outdated(mock_nodes, bot):
    mock_nodes.return_value = [_OUTDATED_NODE]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_skips_bot_authors(mock_nodes, bot):
    mock_nodes.return_value = [_BOT_THREAD_NODE]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_skips_empty_comments(mock_nodes, bot):
    mock_nodes.return_value = [_EMPTY_COMMENTS_NODE]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_head_author_missing(mock_nodes, bot):
    """Head comment with no author field → empty string, does NOT end with [bot]."""
    node = {
        "id": "n-no-author",
        "isResolved": False,
        "isOutdated": False,
        "path": "src/x.py",
        "line": 1,
        "comments": {
            "nodes": [
                {
                    "databaseId": 5,
                    "body": "comment",
                    "diffHunk": "",
                    "createdAt": "2025-01-01T00:00:00Z",
                }
            ]
        },
    }
    mock_nodes.return_value = [node]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 1
    assert threads[0].id == "n-no-author"


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_head_author_null(mock_nodes, bot):
    """Head comment with author=None → empty string, does NOT end with [bot]."""
    node = {
        "id": "n-null-author",
        "isResolved": False,
        "isOutdated": False,
        "path": "src/y.py",
        "line": 2,
        "comments": {
            "nodes": [
                {
                    "databaseId": 6,
                    "author": None,
                    "body": "comment",
                    "diffHunk": "",
                    "createdAt": "2025-01-01T00:00:00Z",
                }
            ]
        },
    }
    mock_nodes.return_value = [node]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 1
    assert threads[0].id == "n-null-author"


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_unresolved_threads_mixed_scenarios(mock_nodes, bot):
    """Integration-style test with a mix of all scenarios."""
    mock_nodes.return_value = [
        _UNRESOLVED_NODE,
        _RESOLVED_NODE,
        _OUTDATED_NODE,
        _BOT_THREAD_NODE,
        _EMPTY_COMMENTS_NODE,
    ]

    threads = list_unresolved_threads(bot, "o/r", 1)

    assert len(threads) == 1
    assert threads[0].id == "n-unresolved"


# ---------------------------------------------------------------------------
# list_resolved_threads
# ---------------------------------------------------------------------------


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_resolved_threads_only_resolved_non_outdated(mock_nodes, bot):
    mock_nodes.return_value = [_RESOLVED_NODE, _UNRESOLVED_NODE, _OUTDATED_NODE]

    threads = list_resolved_threads(bot, "o/r", 1)

    assert len(threads) == 1
    assert threads[0].id == "n-resolved"


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_resolved_threads_skips_unresolved(mock_nodes, bot):
    mock_nodes.return_value = [_UNRESOLVED_NODE]

    threads = list_resolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_resolved_threads_skips_outdated(mock_nodes, bot):
    """Resolved + outdated should still be skipped (outdated trumps)."""
    resolved_and_outdated = {
        "id": "n-res-out",
        "isResolved": True,
        "isOutdated": True,
        "path": "src/z.py",
        "line": 1,
        "comments": {
            "nodes": [
                {
                    "databaseId": 7,
                    "author": {"login": "human"},
                    "body": "old and resolved",
                    "diffHunk": "",
                    "createdAt": "2025-01-01T00:00:00Z",
                }
            ]
        },
    }
    mock_nodes.return_value = [resolved_and_outdated]

    threads = list_resolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_resolved_threads_skips_empty_comments(mock_nodes, bot):
    mock_nodes.return_value = [_EMPTY_COMMENTS_NODE]

    threads = list_resolved_threads(bot, "o/r", 1)

    assert len(threads) == 0


@patch("cai.github.pr._get_review_threads_nodes")
def test_list_resolved_threads_includes_bot_authors(mock_nodes, bot):
    """list_resolved_threads does NOT filter by bot authors — it shows all resolved."""
    bot_node = {
        "id": "n-bot-resolved",
        "isResolved": True,
        "isOutdated": False,
        "path": "src/w.py",
        "line": 5,
        "comments": {
            "nodes": [
                {
                    "databaseId": 8,
                    "author": {"login": "cai[bot]"},
                    "body": "auto fix applied",
                    "diffHunk": "@@ ... @@",
                    "createdAt": "2025-01-05T00:00:00Z",
                }
            ]
        },
    }
    mock_nodes.return_value = [bot_node]

    threads = list_resolved_threads(bot, "o/r", 1)

    assert len(threads) == 1
    assert threads[0].id == "n-bot-resolved"


# ---------------------------------------------------------------------------
# _graphql
# ---------------------------------------------------------------------------


@patch("cai.github.pr.requests.post")
def test_graphql_derives_repo_from_variables(mock_post, bot):
    """_graphql derives the repo string from variables['owner']/variables['name']."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {"some": "payload"}}
    mock_post.return_value = mock_resp

    result = _graphql(bot, "query { foo }", {"owner": "acme", "name": "widgets"})

    assert result == {"some": "payload"}
    # Verify token_for was called with the derived repo
    bot.token_for.assert_called_once_with("acme/widgets")
    # Verify the POST URL and payload
    mock_post.assert_called_once()
    call_args, call_kwargs = mock_post.call_args
    assert call_args[0] == "https://api.github.com/graphql"
    assert call_kwargs["json"]["query"] == "query { foo }"
    assert call_kwargs["json"]["variables"] == {"owner": "acme", "name": "widgets"}


@patch("cai.github.pr.requests.post")
def test_graphql_passes_correct_headers(mock_post, bot):
    """_graphql sets Authorization, Accept, and X-GitHub-Api-Version headers."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"data": {}}
    mock_post.return_value = mock_resp

    _graphql(bot, "q", {"owner": "x", "name": "y"})

    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer fake-token"
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


@patch("cai.github.pr.requests.post")
def test_graphql_raises_on_http_error(mock_post, bot):
    """_graphql raises HTTPError via raise_for_status()."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = requests.HTTPError("500 Server Error")
    mock_post.return_value = mock_resp

    with pytest.raises(requests.HTTPError, match="500 Server Error"):
        _graphql(bot, "q", {"owner": "x", "name": "y"})


@patch("cai.github.pr.requests.post")
def test_graphql_raises_on_errors_key(mock_post, bot):
    """_graphql raises RuntimeError when the response contains 'errors'."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status.return_value = None
    mock_resp.json.return_value = {"errors": [{"message": "bad query"}]}
    mock_post.return_value = mock_resp

    with pytest.raises(RuntimeError, match="GraphQL error"):
        _graphql(bot, "q", {"owner": "x", "name": "y"})


def test_graphql_missing_owner_key_raises(bot):
    """_graphql raises KeyError if 'owner' is missing from variables."""
    with pytest.raises(KeyError):
        _graphql(bot, "q", {"name": "repo"})


def test_graphql_missing_name_key_raises(bot):
    """_graphql raises KeyError if 'name' is missing from variables."""
    with pytest.raises(KeyError):
        _graphql(bot, "q", {"owner": "acme"})


def test_graphql_empty_variables_raises(bot):
    """_graphql raises KeyError with empty variables dict."""
    with pytest.raises(KeyError):
        _graphql(bot, "q", {})


# ---------------------------------------------------------------------------
# resolve_review_thread
# ---------------------------------------------------------------------------


@patch("cai.github.pr._graphql")
def test_resolve_review_thread_splits_repo_and_injects_owner_name(
    mock_graphql, bot
):
    """resolve_review_thread splits 'owner/name' and adds to variables dict."""
    resolve_review_thread(bot, "acme/widgets", "thread-42")

    mock_graphql.assert_called_once()
    _, _, variables = mock_graphql.call_args[0]
    assert variables["threadId"] == "thread-42"
    assert variables["owner"] == "acme"
    assert variables["name"] == "widgets"


@patch("cai.github.pr._graphql")
def test_resolve_review_thread_with_org_repo(mock_graphql, bot):
    """resolve_review_thread handles org/repo with multiple path segments."""
    resolve_review_thread(bot, "my-org/my-team/repo", "thread-99")

    mock_graphql.assert_called_once()
    _, _, variables = mock_graphql.call_args[0]
    assert variables["threadId"] == "thread-99"
    # split("/", 1) splits only on the first '/'
    assert variables["owner"] == "my-org"
    assert variables["name"] == "my-team/repo"


@patch("cai.github.pr._graphql")
def test_resolve_review_thread_correct_query(mock_graphql, bot):
    """resolve_review_thread passes the resolveReviewThread mutation query."""
    resolve_review_thread(bot, "o/r", "thread-1")

    _, query, _ = mock_graphql.call_args[0]
    assert "resolveReviewThread" in query
    assert "threadId" in query


def test_resolve_review_thread_no_slash_in_repo_raises(bot):
    """resolve_review_thread raises ValueError if repo has no '/'."""
    with pytest.raises(ValueError):
        resolve_review_thread(bot, "no-slash-repo", "thread-1")


def test_resolve_review_thread_empty_repo_raises(bot):
    """resolve_review_thread raises ValueError on empty repo string."""
    with pytest.raises(ValueError):
        resolve_review_thread(bot, "", "thread-1")
