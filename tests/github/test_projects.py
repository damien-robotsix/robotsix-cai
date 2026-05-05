from unittest.mock import Mock, patch

import pytest

from cai.github import projects as projects_mod
from cai.github.projects import (
    Ticket,
    create_draft_ticket,
    find_tickets_by_status,
    get_issue_type,
    is_enabled,
    list_tickets,
    promote_ticket_to_issue,
    set_status,
    set_type,
)


@pytest.fixture(autouse=True)
def _clear_project_cache():
    projects_mod._clear_meta_cache()
    yield
    projects_mod._clear_meta_cache()


def _project_bot(owner_type: str = "user") -> Mock:
    """Bot with project config populated."""
    bot = Mock()
    bot.app_id = 42
    bot.project_owner = "damien-robotsix"
    bot.project_number = 7
    bot.project_owner_type = owner_type
    bot.project_default_repo = "damien-robotsix/robotsix-cai"
    bot.token_for.return_value = "tok"
    return bot


def _gql_response(items_with_field_values):
    """Build a GraphQL payload mirroring the projectItems → fieldValues shape."""
    return {
        "data": {
            "repository": {
                "issue": {
                    "projectItems": {"nodes": items_with_field_values}
                }
            }
        }
    }


def _bot():
    bot = Mock()
    bot.token_for.return_value = "tok"
    return bot


def test_returns_type_value_when_field_present():
    bot = _bot()
    payload = _gql_response([
        {"fieldValues": {"nodes": [
            {"name": "analysis", "field": {"name": "Type"}},
            {"name": "Backlog", "field": {"name": "Status"}},
        ]}}
    ])
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = payload

    with patch("cai.github.projects.requests.post", return_value=mock_resp) as mock_post:
        assert get_issue_type(bot, "o/r", 42) == "analysis"

    sent = mock_post.call_args
    assert sent.kwargs["json"]["variables"] == {"owner": "o", "name": "r", "number": 42}
    assert sent.kwargs["headers"]["Authorization"] == "Bearer tok"


def test_returns_none_when_no_type_field():
    bot = _bot()
    payload = _gql_response([
        {"fieldValues": {"nodes": [
            {"name": "Backlog", "field": {"name": "Status"}},
        ]}}
    ])
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = payload

    with patch("cai.github.projects.requests.post", return_value=mock_resp):
        assert get_issue_type(bot, "o/r", 42) is None


def test_returns_none_when_no_project_items():
    bot = _bot()
    payload = _gql_response([])
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = payload

    with patch("cai.github.projects.requests.post", return_value=mock_resp):
        assert get_issue_type(bot, "o/r", 42) is None


def test_returns_none_on_graphql_errors():
    """Insufficient project scope → errors block. Treat as 'no Type set'."""
    bot = _bot()
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = {
        "errors": [{"message": "Resource not accessible by integration"}],
        "data": None,
    }

    with patch("cai.github.projects.requests.post", return_value=mock_resp):
        assert get_issue_type(bot, "o/r", 42) is None


def test_returns_none_on_http_error():
    bot = _bot()
    mock_resp = Mock(status_code=502)

    with patch("cai.github.projects.requests.post", return_value=mock_resp):
        assert get_issue_type(bot, "o/r", 42) is None


def test_handles_null_issue_in_payload():
    """If GraphQL returns repository.issue=null (e.g. wrong number), don't crash."""
    bot = _bot()
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = {
        "data": {"repository": {"issue": None}}
    }

    with patch("cai.github.projects.requests.post", return_value=mock_resp):
        assert get_issue_type(bot, "o/r", 42) is None


def test_first_matching_project_wins():
    bot = _bot()
    payload = _gql_response([
        {"fieldValues": {"nodes": [
            {"name": "code-change", "field": {"name": "Type"}},
        ]}},
        {"fieldValues": {"nodes": [
            {"name": "analysis", "field": {"name": "Type"}},
        ]}},
    ])
    mock_resp = Mock(status_code=200)
    mock_resp.json.return_value = payload

    with patch("cai.github.projects.requests.post", return_value=mock_resp):
        assert get_issue_type(bot, "o/r", 42) == "code-change"


def test_rejects_malformed_repo():
    bot = _bot()
    with pytest.raises(ValueError, match="expected owner/repo"):
        get_issue_type(bot, "invalid", 42)


# ---------------------------------------------------------------------------
# is_enabled / config presence
# ---------------------------------------------------------------------------


class TestIsEnabled:
    def test_true_when_owner_and_number_set(self):
        bot = _project_bot()
        assert is_enabled(bot) is True

    def test_false_when_owner_missing(self):
        bot = _project_bot()
        bot.project_owner = None
        assert is_enabled(bot) is False

    def test_false_when_number_missing(self):
        bot = _project_bot()
        bot.project_number = None
        assert is_enabled(bot) is False


# ---------------------------------------------------------------------------
# _resolve_project_meta + caching
# ---------------------------------------------------------------------------


def _resolve_payload(owner_key: str = "user") -> dict:
    return {
        "data": {
            owner_key: {
                "projectV2": {
                    "id": "PVT_1",
                    "fields": {
                        "nodes": [
                            {
                                "id": "FLD_TYPE",
                                "name": "Type",
                                "options": [
                                    {"id": "OPT_CC", "name": "code-change"},
                                    {"id": "OPT_AN", "name": "analysis"},
                                ],
                            },
                            {
                                "id": "FLD_STATUS",
                                "name": "Status",
                                "options": [
                                    {"id": "OPT_BL", "name": "Backlog"},
                                    {"id": "OPT_REF", "name": "Refined"},
                                    {"id": "OPT_RD", "name": "Ready"},
                                    {"id": "OPT_IP", "name": "In Progress"},
                                    {"id": "OPT_IRV", "name": "In Review"},
                                    {"id": "OPT_DN", "name": "Done"},
                                ],
                            },
                            {
                                "id": "FLD_APPROVED",
                                "name": "Approved",
                                "options": [{"id": "OPT_AYES", "name": "Yes"}],
                            },
                            {
                                "id": "FLD_REBASE",
                                "name": "Needs Rebase",
                                "options": [{"id": "OPT_RYES", "name": "Yes"}],
                            },
                            {"id": "FLD_TITLE", "name": "Title"},
                        ]
                    },
                }
            }
        }
    }


class TestResolveProjectMeta:
    def test_resolves_user_project(self):
        bot = _project_bot("user")
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _resolve_payload("user")
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            meta = projects_mod._resolve_project_meta(bot)

        assert meta.project_id == "PVT_1"
        assert meta.field_ids["Type"] == "FLD_TYPE"
        assert meta.field_ids["Status"] == "FLD_STATUS"
        assert meta.field_options["Type"] == {"code-change": "OPT_CC", "analysis": "OPT_AN"}
        assert meta.field_options["Status"]["Ready"] == "OPT_RD"

    def test_resolves_org_project(self):
        bot = _project_bot("organization")
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _resolve_payload("organization")
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp) as mock_post:
            projects_mod._resolve_project_meta(bot)

        # Org query path was selected.
        sent_query = mock_post.call_args.kwargs["json"]["query"]
        assert "organization(login:" in sent_query

    def test_caches_per_bot(self):
        bot = _project_bot()
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _resolve_payload()
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp) as mock_post:
            projects_mod._resolve_project_meta(bot)
            projects_mod._resolve_project_meta(bot)

        # One HTTP call despite two resolve calls.
        assert mock_post.call_count == 1

    def test_raises_when_not_enabled(self):
        bot = _project_bot()
        bot.project_number = None
        with pytest.raises(RuntimeError, match="not configured"):
            projects_mod._resolve_project_meta(bot)

    def test_raises_when_project_not_found(self):
        bot = _project_bot()
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = {"data": {"user": {"projectV2": None}}}
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Project not found"):
                projects_mod._resolve_project_meta(bot)


# ---------------------------------------------------------------------------
# create_draft_ticket
# ---------------------------------------------------------------------------


def _stub_resolve(bot, post_mock, *, with_resolve_first=True):
    """Make the first GraphQL POST return the resolve payload, subsequent ones empty data."""
    resolve = Mock(status_code=200)
    resolve.json.return_value = _resolve_payload()
    resolve.raise_for_status = Mock()

    add = Mock(status_code=200)
    add.json.return_value = {
        "data": {"addProjectV2DraftIssue": {"projectItem": {"id": "PVTI_99"}}}
    }
    add.raise_for_status = Mock()

    update = Mock(status_code=200)
    update.json.return_value = {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_99"}}}}
    update.raise_for_status = Mock()

    side_effects = ([resolve] if with_resolve_first else []) + [add, update, update]
    post_mock.side_effect = side_effects
    return resolve, add, update


class TestCreateDraftTicket:
    def test_creates_draft_with_type_and_default_status(self):
        bot = _project_bot()
        with patch("cai.github.projects.requests.post") as mock_post:
            _stub_resolve(bot, mock_post)

            item_id = create_draft_ticket(
                bot, title="T", body="B", type="analysis"
            )

            assert item_id == "PVTI_99"
            # Three calls: resolve + addDraft + setType + setStatus = 4
            assert mock_post.call_count == 4

            # Verify Type was set to analysis option id.
            type_call = mock_post.call_args_list[2]
            type_vars = type_call.kwargs["json"]["variables"]
            assert type_vars["fieldId"] == "FLD_TYPE"
            assert type_vars["optionId"] == "OPT_AN"

            # Verify Status was set to Backlog option id.
            status_call = mock_post.call_args_list[3]
            status_vars = status_call.kwargs["json"]["variables"]
            assert status_vars["fieldId"] == "FLD_STATUS"
            assert status_vars["optionId"] == "OPT_BL"

    def test_creates_draft_with_explicit_status(self):
        bot = _project_bot()
        with patch("cai.github.projects.requests.post") as mock_post:
            _stub_resolve(bot, mock_post)

            create_draft_ticket(
                bot, title="T", body="B", type="code-change", status="Ready"
            )

            status_call = mock_post.call_args_list[3]
            assert status_call.kwargs["json"]["variables"]["optionId"] == "OPT_RD"

    def test_rejects_invalid_type(self):
        bot = _project_bot()
        with pytest.raises(ValueError, match="type must be one of"):
            create_draft_ticket(bot, title="T", body="B", type="weird")

    def test_rejects_unknown_status(self):
        bot = _project_bot()
        with patch("cai.github.projects.requests.post") as mock_post:
            resolve = Mock(status_code=200)
            resolve.json.return_value = _resolve_payload()
            resolve.raise_for_status = Mock()
            add = Mock(status_code=200)
            add.json.return_value = {
                "data": {"addProjectV2DraftIssue": {"projectItem": {"id": "PVTI_99"}}}
            }
            add.raise_for_status = Mock()
            update = Mock(status_code=200)
            update.json.return_value = {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_99"}}}}
            update.raise_for_status = Mock()
            mock_post.side_effect = [resolve, add, update]

            with pytest.raises(RuntimeError, match="no option 'Triage'"):
                create_draft_ticket(
                    bot, title="T", body="B", type="analysis", status="Triage"
                )


# ---------------------------------------------------------------------------
# set_status / set_type
# ---------------------------------------------------------------------------


class TestSetters:
    def test_set_status_resolves_option_id(self):
        bot = _project_bot()
        with patch("cai.github.projects.requests.post") as mock_post:
            resolve = Mock(status_code=200)
            resolve.json.return_value = _resolve_payload()
            resolve.raise_for_status = Mock()
            update = Mock(status_code=200)
            update.json.return_value = {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_X"}}}}
            update.raise_for_status = Mock()
            mock_post.side_effect = [resolve, update]

            set_status(bot, "PVTI_X", "In Progress")

            update_vars = mock_post.call_args_list[1].kwargs["json"]["variables"]
            assert update_vars["itemId"] == "PVTI_X"
            assert update_vars["fieldId"] == "FLD_STATUS"
            assert update_vars["optionId"] == "OPT_IP"

    def test_set_type(self):
        bot = _project_bot()
        with patch("cai.github.projects.requests.post") as mock_post:
            resolve = Mock(status_code=200)
            resolve.json.return_value = _resolve_payload()
            resolve.raise_for_status = Mock()
            update = Mock(status_code=200)
            update.json.return_value = {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PVTI_X"}}}}
            update.raise_for_status = Mock()
            mock_post.side_effect = [resolve, update]

            set_type(bot, "PVTI_X", "code-change")

            update_vars = mock_post.call_args_list[1].kwargs["json"]["variables"]
            assert update_vars["fieldId"] == "FLD_TYPE"
            assert update_vars["optionId"] == "OPT_CC"


# ---------------------------------------------------------------------------
# set_flag (Approved / Needs Rebase)
# ---------------------------------------------------------------------------


class TestSetFlag:
    def _resolve_then_op_responses(self):
        resolve = Mock(status_code=200)
        resolve.json.return_value = _resolve_payload()
        resolve.raise_for_status = Mock()
        op = Mock(status_code=200)
        op.json.return_value = {"data": {"x": {"projectV2Item": {"id": "PVTI_X"}}}}
        op.raise_for_status = Mock()
        return resolve, op

    def test_set_flag_true_sets_yes_option(self):
        from cai.github.projects import set_flag

        bot = _project_bot()
        resolve, op = self._resolve_then_op_responses()
        with patch("cai.github.projects.requests.post", side_effect=[resolve, op]) as mock_post:
            set_flag(bot, "PVTI_X", "Approved", True)

            vars = mock_post.call_args_list[1].kwargs["json"]["variables"]
            assert vars["fieldId"] == "FLD_APPROVED"
            assert vars["optionId"] == "OPT_AYES"
            # Used the update mutation, not the clear mutation.
            assert "updateProjectV2ItemFieldValue" in mock_post.call_args_list[1].kwargs["json"]["query"]

    def test_set_flag_false_clears_field(self):
        from cai.github.projects import set_flag

        bot = _project_bot()
        resolve, op = self._resolve_then_op_responses()
        with patch("cai.github.projects.requests.post", side_effect=[resolve, op]) as mock_post:
            set_flag(bot, "PVTI_X", "Needs Rebase", False)

            sent_query = mock_post.call_args_list[1].kwargs["json"]["query"]
            assert "clearProjectV2ItemFieldValue" in sent_query

    def test_set_flag_unknown_field_raises(self):
        from cai.github.projects import set_flag

        bot = _project_bot()
        resolve = Mock(status_code=200)
        resolve.json.return_value = _resolve_payload()
        resolve.raise_for_status = Mock()
        with patch("cai.github.projects.requests.post", return_value=resolve):
            with pytest.raises(RuntimeError, match="no field 'Bogus'"):
                set_flag(bot, "PVTI_X", "Bogus", True)


# ---------------------------------------------------------------------------
# Flag-aware Ticket parsing + cron helpers
# ---------------------------------------------------------------------------


def _node_with_flags(item_id, status, approved=False, needs_rebase=False, type_value="code-change"):
    field_values = [
        {"name": type_value, "field": {"name": "Type"}},
        {"name": status, "field": {"name": "Status"}},
    ]
    if approved:
        field_values.append({"name": "Yes", "field": {"name": "Approved"}})
    if needs_rebase:
        field_values.append({"name": "Yes", "field": {"name": "Needs Rebase"}})
    return {
        "id": item_id,
        "isArchived": False,
        "fieldValues": {"nodes": field_values},
        "content": {"__typename": "DraftIssue", "title": "T", "body": "B"},
    }


class TestFlagsOnTickets:
    def test_ticket_carries_approved_and_rebase_flags(self):
        bot = _project_bot()
        items = [
            _node_with_flags("PVTI_1", "In Review", approved=True),
            _node_with_flags("PVTI_2", "In Review", approved=False, needs_rebase=True),
            _node_with_flags("PVTI_3", "Backlog"),
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            tickets = list_tickets(bot)

        by_id = {t.item_id: t for t in tickets}
        assert by_id["PVTI_1"].approved is True
        assert by_id["PVTI_1"].needs_rebase is False
        assert by_id["PVTI_2"].approved is False
        assert by_id["PVTI_2"].needs_rebase is True
        assert by_id["PVTI_3"].approved is False
        assert by_id["PVTI_3"].needs_rebase is False

    def test_find_tickets_pending_merge(self):
        from cai.github.projects import find_tickets_pending_merge

        bot = _project_bot()
        items = [
            _node_with_flags("PVTI_1", "In Review", approved=True),
            _node_with_flags("PVTI_2", "In Review", approved=False),
            _node_with_flags("PVTI_3", "Ready", approved=True),  # wrong status
            _node_with_flags("PVTI_4", "In Review", approved=True, needs_rebase=True),  # also rebases — still pending merge
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            pending = find_tickets_pending_merge(bot)

        assert {t.item_id for t in pending} == {"PVTI_1", "PVTI_4"}

    def test_find_tickets_pending_rebase(self):
        from cai.github.projects import find_tickets_pending_rebase

        bot = _project_bot()
        items = [
            _node_with_flags("PVTI_1", "In Review", needs_rebase=True),
            _node_with_flags("PVTI_2", "In Review"),
            _node_with_flags("PVTI_3", "Ready", needs_rebase=True),  # any status counts
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            pending = find_tickets_pending_rebase(bot)

        assert {t.item_id for t in pending} == {"PVTI_1", "PVTI_3"}


# ---------------------------------------------------------------------------
# list_tickets / find_tickets_by_status
# ---------------------------------------------------------------------------


def _list_payload(items, has_next=False, end_cursor=None, owner_key="user"):
    return {
        "data": {
            owner_key: {
                "projectV2": {
                    "items": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                        "nodes": items,
                    }
                }
            }
        }
    }


def _draft_node(item_id, type_value, status_value, title="T", body="B", archived=False):
    return {
        "id": item_id,
        "isArchived": archived,
        "fieldValues": {
            "nodes": [
                {"name": type_value, "field": {"name": "Type"}} if type_value else {},
                {"name": status_value, "field": {"name": "Status"}} if status_value else {},
            ]
        },
        "content": {"__typename": "DraftIssue", "title": title, "body": body},
    }


class TestListTickets:
    def test_returns_empty_when_not_enabled(self):
        bot = _project_bot()
        bot.project_number = None
        assert list_tickets(bot) == []

    def test_returns_parsed_tickets(self):
        bot = _project_bot()
        items = [
            _draft_node("PVTI_1", "analysis", "Backlog", title="A"),
            _draft_node("PVTI_2", "code-change", "Ready", title="B"),
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            tickets = list_tickets(bot)

        assert len(tickets) == 2
        assert tickets[0].item_id == "PVTI_1"
        assert tickets[0].type == "analysis"
        assert tickets[0].status == "Backlog"
        assert tickets[1].type == "code-change"
        assert tickets[1].status == "Ready"

    def test_skips_archived_items(self):
        bot = _project_bot()
        items = [
            _draft_node("PVTI_1", "analysis", "Backlog", archived=True),
            _draft_node("PVTI_2", "analysis", "Backlog"),
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            tickets = list_tickets(bot)

        assert [t.item_id for t in tickets] == ["PVTI_2"]

    def test_pages_through_results(self):
        bot = _project_bot()
        page1 = Mock(status_code=200)
        page1.json.return_value = _list_payload(
            [_draft_node("PVTI_1", "analysis", "Backlog")],
            has_next=True,
            end_cursor="C1",
        )
        page1.raise_for_status = Mock()
        page2 = Mock(status_code=200)
        page2.json.return_value = _list_payload(
            [_draft_node("PVTI_2", "code-change", "Ready")]
        )
        page2.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", side_effect=[page1, page2]) as mock_post:
            tickets = list_tickets(bot)

        assert [t.item_id for t in tickets] == ["PVTI_1", "PVTI_2"]
        # Second call passed the cursor.
        assert mock_post.call_args_list[1].kwargs["json"]["variables"]["cursor"] == "C1"

    def test_find_tickets_by_status(self):
        bot = _project_bot()
        items = [
            _draft_node("PVTI_1", "analysis", "Backlog"),
            _draft_node("PVTI_2", "code-change", "Ready"),
            _draft_node("PVTI_3", "analysis", "Ready"),
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            ready = find_tickets_by_status(bot, "Ready")

        assert {t.item_id for t in ready} == {"PVTI_2", "PVTI_3"}

    def test_handles_issue_content(self):
        bot = _project_bot()
        items = [
            {
                "id": "PVTI_X",
                "isArchived": False,
                "fieldValues": {"nodes": [
                    {"name": "code-change", "field": {"name": "Type"}},
                    {"name": "Done", "field": {"name": "Status"}},
                ]},
                "content": {
                    "__typename": "Issue",
                    "title": "Real issue",
                    "number": 42,
                    "url": "https://github.com/o/r/issues/42",
                    "repository": {"nameWithOwner": "o/r"},
                },
            }
        ]
        mock_resp = Mock(status_code=200)
        mock_resp.json.return_value = _list_payload(items)
        mock_resp.raise_for_status = Mock()

        with patch("cai.github.projects.requests.post", return_value=mock_resp):
            tickets = list_tickets(bot)

        assert tickets[0].content_type == "Issue"
        assert tickets[0].issue_number == 42
        assert tickets[0].issue_repo == "o/r"
        assert tickets[0].issue_url == "https://github.com/o/r/issues/42"


# ---------------------------------------------------------------------------
# promote_ticket_to_issue
# ---------------------------------------------------------------------------


class TestPromote:
    def test_resolves_repo_id_then_calls_convert(self):
        bot = _project_bot()
        repo_resp = Mock(status_code=200)
        repo_resp.json.return_value = {"data": {"repository": {"id": "REPO_1"}}}
        repo_resp.raise_for_status = Mock()
        convert_resp = Mock(status_code=200)
        convert_resp.json.return_value = {
            "data": {
                "convertProjectV2DraftIssueItemToIssue": {
                    "item": {"content": {"number": 100, "url": "https://gh/o/r/issues/100"}}
                }
            }
        }
        convert_resp.raise_for_status = Mock()

        # bot.repo(...).get_issue(100) returns a Mock issue.
        mock_issue = Mock()
        bot.repo.return_value.get_issue.return_value = mock_issue

        with patch("cai.github.projects.requests.post", side_effect=[repo_resp, convert_resp]):
            issue = promote_ticket_to_issue(bot, "PVTI_X")

        assert issue is mock_issue
        bot.repo.assert_called_with("damien-robotsix/robotsix-cai")
        bot.repo.return_value.get_issue.assert_called_with(100)

    def test_uses_explicit_repo_when_provided(self):
        bot = _project_bot()
        bot.project_default_repo = None
        repo_resp = Mock(status_code=200)
        repo_resp.json.return_value = {"data": {"repository": {"id": "REPO_X"}}}
        repo_resp.raise_for_status = Mock()
        convert_resp = Mock(status_code=200)
        convert_resp.json.return_value = {
            "data": {
                "convertProjectV2DraftIssueItemToIssue": {
                    "item": {"content": {"number": 5}}
                }
            }
        }
        convert_resp.raise_for_status = Mock()
        bot.repo.return_value.get_issue.return_value = Mock()

        with patch("cai.github.projects.requests.post", side_effect=[repo_resp, convert_resp]) as mock_post:
            promote_ticket_to_issue(bot, "PVTI_X", repo="other/repo")

        repo_vars = mock_post.call_args_list[0].kwargs["json"]["variables"]
        assert repo_vars["owner"] == "other"
        assert repo_vars["name"] == "repo"

    def test_raises_when_no_target_repo(self):
        bot = _project_bot()
        bot.project_default_repo = None
        with pytest.raises(RuntimeError, match="no target repo"):
            promote_ticket_to_issue(bot, "PVTI_X")
