"""Tests for helper functions in cai.log.traces."""

from __future__ import annotations

from datetime import datetime, timezone

from cai.log.traces import _build_list_kwargs, _format_failures, _format_trace


class TestFormatTrace:
    """Tests for _format_trace(t) — the dict builder that maps a trace object
    to a five-field dict.
    """

    def test_all_fields_present_and_correct(self):
        """Happy-path: all attributes set, all five fields correct."""
        t = _FakeTrace(
            id="tr-001",
            name="cai-solve",
            timestamp=datetime(2025, 1, 15, 10, 30, 0, tzinfo=timezone.utc),
            total_cost=0.042,
            latency=3.7,
        )
        result = _format_trace(t)
        assert result == {
            "id": "tr-001",
            "name": "cai-solve",
            "timestamp": "2025-01-15T10:30:00+00:00",
            "cost": 0.042,
            "latency": 3.7,
        }

    def test_name_falls_back_to_empty_string_when_none(self):
        """``name`` becomes ``""`` when the trace name is None."""
        t = _FakeTrace(id="tr-002", name=None)
        result = _format_trace(t)
        assert result["name"] == ""

    def test_timestamp_none_when_attribute_missing(self):
        """``timestamp`` is None when the object has no timestamp attribute."""
        t = _FakeTrace(id="tr-003", name="wf", _omit={"timestamp"})
        result = _format_trace(t)
        assert result["timestamp"] is None

    def test_timestamp_none_when_explicit_none(self):
        """``timestamp`` is None when timestamp is explicitly None."""
        t = _FakeTrace(id="tr-004", name="wf", timestamp=None)
        result = _format_trace(t)
        assert result["timestamp"] is None

    def test_cost_none_when_attribute_missing(self):
        """``cost`` is None when the object has no total_cost attribute."""
        t = _FakeTrace(id="tr-005", name="wf", _omit={"total_cost"})
        result = _format_trace(t)
        assert result["cost"] is None

    def test_latency_none_when_attribute_missing(self):
        """``latency`` is None when the object has no latency attribute."""
        t = _FakeTrace(id="tr-006", name="wf", _omit={"latency"})
        result = _format_trace(t)
        assert result["latency"] is None

    def test_cost_zero_is_preserved(self):
        """``cost`` is 0 when total_cost is 0 (zero is a valid cost)."""
        t = _FakeTrace(id="tr-007", name="wf", total_cost=0)
        result = _format_trace(t)
        assert result["cost"] == 0

    def test_latency_zero_is_preserved(self):
        """``latency`` is 0 when latency is 0."""
        t = _FakeTrace(id="tr-008", name="wf", latency=0)
        result = _format_trace(t)
        assert result["latency"] == 0

    def test_dict_has_exactly_five_keys(self):
        """The returned dict has exactly the five expected keys."""
        t = _FakeTrace(id="tr-009", name="wf")
        result = _format_trace(t)
        assert set(result.keys()) == {"id", "name", "timestamp", "cost", "latency"}
        assert len(result) == 5


class TestFakeTrace:
    """Tests for _FakeTrace.__init__ — especially _omit AttributeError handling."""

    def test_omit_deletes_attribute_when_present(self):
        """_omit removes an attribute that was set by kwargs."""
        t = _FakeTrace(id="t1", name="wf", timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                       _omit={"timestamp"})
        assert not hasattr(t, "timestamp")
        assert t.id == "t1"
        assert t.name == "wf"

    def test_omit_silently_skips_attribute_never_set(self):
        """_omit does not raise when the attribute was never passed to kwargs."""
        t = _FakeTrace(id="t1", name="wf", _omit={"total_cost", "latency"})
        assert not hasattr(t, "total_cost")
        assert not hasattr(t, "latency")
        assert t.id == "t1"
        assert t.name == "wf"

    def test_omit_mixed_some_present_some_missing(self):
        """Mixed _omit: present attrs are deleted, missing ones are silently skipped."""
        t = _FakeTrace(id="t1", name="wf", timestamp=None, latency=1.5,
                       _omit={"timestamp", "total_cost"})
        # timestamp was passed (as None) → deleted
        assert not hasattr(t, "timestamp")
        # total_cost was never passed → silently skipped (no AttributeError)
        assert not hasattr(t, "total_cost")
        # latency was passed and not in _omit → present
        assert t.latency == 1.5
        # id and name always present
        assert t.id == "t1"
        assert t.name == "wf"

    def test_empty_omit_has_no_effect(self):
        """Empty _omit (default) leaves all kwargs as attributes."""
        t = _FakeTrace(id="t1", name="wf", total_cost=0.0, latency=0.0)
        assert t.id == "t1"
        assert t.name == "wf"
        assert t.total_cost == 0.0
        assert t.latency == 0.0

    def test_no_omit_keyword_uses_default_empty_set(self):
        """When _omit is not passed at all, all kwargs become attributes."""
        t = _FakeTrace(id="t1", name="wf", total_cost=1.23)
        assert hasattr(t, "total_cost")
        assert t.total_cost == 1.23


class TestBuildListKwargs:
    """Tests for _build_list_kwargs(limit, since) — the shared kwargs builder."""

    def test_basic_without_since(self):
        """Returns dict with limit and page=1 when since is None."""
        result = _build_list_kwargs(20)
        assert result == {"limit": 20, "page": 1}

    def test_with_since(self):
        """Sets a UTC-aware from_timestamp when since is an ISO string."""
        result = _build_list_kwargs(50, since="2025-06-01T12:00:00")
        assert result["limit"] == 50
        assert result["page"] == 1
        ts = result["from_timestamp"]
        assert ts == datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_since_none_explicit(self):
        """When since is explicitly None no from_timestamp key is added."""
        result = _build_list_kwargs(10, since=None)
        assert "from_timestamp" not in result
        assert result == {"limit": 10, "page": 1}

    def test_returned_dict_is_mutable(self):
        """Callers can safely add extra keys to the returned dict."""
        result = _build_list_kwargs(5, since="2025-01-01")
        result["name"] = "cai-solve"
        assert result["name"] == "cai-solve"
        assert result["limit"] == 5
        assert result["page"] == 1
        assert "from_timestamp" in result


class TestFormatFailures:
    """Tests for ``_format_failures()`` — the shared helper that formats
    Langfuse trace failures into display lines."""

    # ── basic rendering ──────────────────────────────────────────────────

    def test_single_failure_all_fields(self):
        """A single failure with all optional fields renders every expected line."""
        failures = [
            {
                "id": "trace-001",
                "name": "cai-solve",
                "timestamp": "2025-06-15T10:30:00+00:00",
                "errors": [
                    {
                        "name": "implement",
                        "status_message": "rate limit exceeded",
                        "output": "Error 429",
                    },
                ],
            },
        ]
        result = _format_failures(failures)
        assert len(result) == 4  # header line not present, 1 failure + 3 error lines
        assert result[0] == "\n[2025-06-15T10:30:00] cai-solve  trace_id=trace-001"
        assert result[1] == "  Failed step: implement"
        assert result[2] == "    Message: rate limit exceeded"
        assert result[3] == "    Output:  Error 429"

    def test_multiple_failures(self):
        """Two failures each appear with their own header line."""
        failures = [
            {
                "id": "trace-a",
                "name": "cai-solve",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "step1", "status_message": "boom", "output": "x"}],
            },
            {
                "id": "trace-b",
                "name": "cai-audit",
                "timestamp": "2025-01-02T00:00:00+00:00",
                "errors": [{"name": "step2", "status_message": "bang", "output": "y"}],
            },
        ]
        result = _format_failures(failures)
        assert result[0] == "\n[2025-01-01T00:00:00] cai-solve  trace_id=trace-a"
        assert result[4] == "\n[2025-01-02T00:00:00] cai-audit  trace_id=trace-b"

    # ── header option ────────────────────────────────────────────────────

    def test_header_prepended(self):
        """When ``header`` is set it becomes the first line."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e"}],
            },
        ]
        result = _format_failures(failures, header="Recent failures (1 traces):")
        assert result[0] == "Recent failures (1 traces):"
        # The first failure line still starts with \n
        assert result[1] == "\n[2025-01-01T00:00:00] w  trace_id=t1"

    def test_no_header_when_none(self):
        """When header is None (default) no extra first line appears."""
        result = _format_failures(
            [{"id": "t1", "name": "w", "timestamp": "2025-01-01T00:00:00+00:00",
              "errors": [{"name": "e"}]}],
            header=None,
        )
        assert not result[0].startswith("Recent")

    # ── empty inputs ─────────────────────────────────────────────────────

    def test_empty_failures_returns_empty_list(self):
        """No failures → empty list (no header)."""
        assert _format_failures([]) == []

    def test_empty_failures_with_header_returns_only_header(self):
        """Empty failures with a header returns just the header line."""
        result = _format_failures([], header="No failures today.")
        assert result == ["No failures today."]

    # ── missing / None fields ────────────────────────────────────────────

    def test_timestamp_none_renders_question_mark(self):
        """None timestamp → ``?`` in the output."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": None,
                "errors": [{"name": "e"}],
            },
        ]
        result = _format_failures(failures)
        assert result[0] == "\n[?] w  trace_id=t1"

    def test_status_message_missing_key(self):
        """Error dict without ``status_message`` key omits the Message line."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "output": "out"}],
            },
        ]
        result = _format_failures(failures)
        # Only 3 lines: failure header, Failed step, Output
        assert len(result) == 3
        assert "Message:" not in "\n".join(result)

    def test_output_missing_key(self):
        """Error dict without ``output`` key omits the Output line."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": "msg"}],
            },
        ]
        result = _format_failures(failures)
        assert "Output:" not in "\n".join(result)

    def test_both_optional_fields_missing(self):
        """Error with only ``name`` renders just the Failed step line."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "bare"}],
            },
        ]
        result = _format_failures(failures)
        assert len(result) == 2  # failure header + Failed step only
        assert result[1] == "  Failed step: bare"

    def test_status_message_none_value_skipped(self):
        """``status_message`` is explicitly None → treated as missing."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": None, "output": "out"}],
            },
        ]
        result = _format_failures(failures)
        assert "Message:" not in "\n".join(result)

    def test_output_none_value_skipped(self):
        """``output`` is explicitly None → treated as missing."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": "msg", "output": None}],
            },
        ]
        result = _format_failures(failures)
        assert "Output:" not in "\n".join(result)

    # ── truncation ───────────────────────────────────────────────────────

    def test_max_message_len_truncates(self):
        """Message longer than ``max_message_len`` is sliced."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": "A" * 100}],
            },
        ]
        result = _format_failures(failures, max_message_len=10)
        assert "    Message: " + "A" * 10 in result
        assert "A" * 100 not in "\n".join(result)

    def test_max_output_len_truncates(self):
        """Output longer than ``max_output_len`` is sliced."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "output": "B" * 100}],
            },
        ]
        result = _format_failures(failures, max_output_len=5)
        assert "    Output:  " + "B" * 5 in result
        assert "B" * 100 not in "\n".join(result)

    def test_no_truncation_when_limits_are_none(self):
        """When both limits are None (default) long values pass through."""
        long_msg = "X" * 500
        long_out = "Y" * 500
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": long_msg, "output": long_out}],
            },
        ]
        result = _format_failures(failures)
        assert long_msg in "\n".join(result)
        assert long_out in "\n".join(result)

    def test_message_shorter_than_limit_not_truncated(self):
        """Message equal to or shorter than limit passes through unchanged."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": "hi"}],
            },
        ]
        result = _format_failures(failures, max_message_len=300)
        assert "    Message: hi" in result

    def test_output_shorter_than_limit_not_truncated(self):
        """Output equal to or shorter than limit passes through unchanged."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "output": "ok"}],
            },
        ]
        result = _format_failures(failures, max_output_len=200)
        assert "    Output:  ok" in result

    # ── multiple errors per failure ──────────────────────────────────────

    def test_multiple_errors_in_one_failure(self):
        """A single trace with multiple errors renders all of them."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [
                    {"name": "err1", "status_message": "m1"},
                    {"name": "err2", "status_message": "m2", "output": "o2"},
                    {"name": "err3"},
                ],
            },
        ]
        result = _format_failures(failures)
        assert len(result) == 7  # 1 failure header + 3×(Failed step) + 2 messages + 1 output
        assert result[1] == "  Failed step: err1"
        assert result[2] == "    Message: m1"
        assert result[3] == "  Failed step: err2"
        assert result[4] == "    Message: m2"
        assert result[5] == "    Output:  o2"
        assert result[6] == "  Failed step: err3"

    # ── integration-style: matches expected audit.py usage ───────────────

    def test_audit_mode_usage(self):
        """Combined header + truncation matches _build_errors_prompt's call."""
        failures = [
            {
                "id": "trace-abc",
                "name": "cai-solve",
                "timestamp": "2025-06-15T10:30:00+00:00",
                "errors": [
                    {
                        "name": "implement",
                        "status_message": "rate limited " * 50,
                        "output": "traceback " * 30,
                    },
                ],
            },
        ]
        result = _format_failures(
            failures,
            max_message_len=300,
            max_output_len=200,
            header="Recent failures (1 traces with errors):",
        )
        assert result[0] == "Recent failures (1 traces with errors):"
        assert "trace-abc" in result[1]
        # Message truncated to 300
        assert len("rate limited " * 50) > 300
        assert "    Message: " + ("rate limited " * 50)[:300] in result
        # Output truncated to 200
        assert len("traceback " * 30) > 200
        assert "    Output:  " + ("traceback " * 30)[:200] in result

    def test_traces_failures_usage(self):
        """No header, no truncation — matches traces_failures() call site."""
        failures = [
            {
                "id": "t1",
                "name": "w",
                "timestamp": "2025-01-01T00:00:00+00:00",
                "errors": [{"name": "e", "status_message": "m", "output": "o"}],
            },
        ]
        result = _format_failures(failures)
        # No header → first line is the failure
        assert result[0].startswith("\n[")
        # Full output preserved
        assert "    Message: m" in result
        assert "    Output:  o" in result


class _FakeTrace:
    """Minimal fake that mimics a Langfuse trace object.

    Any keyword passed as ``_omit`` will be removed from the instance
    *after* ``__init__`` so that ``getattr(t, attr, None)`` sees no
    attribute at all (simulating a trace that simply lacks the field).
    """

    def __init__(self, **kwargs):
        _omit = set(kwargs.pop("_omit", ()))
        for k, v in kwargs.items():
            setattr(self, k, v)
        for k in _omit:
            try:
                delattr(self, k)
            except AttributeError:
                pass
