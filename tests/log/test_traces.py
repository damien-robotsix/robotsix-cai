import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import os

from cai.log.traces import (
    LangfuseTraces,
    traces_list,
    traces_show,
    traces_failures,
    traces_session_cost,
    traces_session,
    _has_error_level,
    _sort_key,
    _obs_dict,
    _obs_error_dict
)

@pytest.fixture
def mock_langfuse():
    with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "pk", "LANGFUSE_SECRET_KEY": "sk"}):
        with patch("cai.log.traces.Langfuse") as mock_langfuse_cls:
            mock_client = MagicMock()
            mock_langfuse_cls.return_value = mock_client
            yield mock_client

@pytest.fixture
def traces_client(mock_langfuse):
    client = LangfuseTraces()
    return client

def test_langfuse_traces_init_missing_env():
    with patch.dict(os.environ, {}, clear=True):
        client = LangfuseTraces()
        with pytest.raises(EnvironmentError, match="LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set"):
            _ = client.client

def test_langfuse_traces_init_with_env(mock_langfuse):
    client = LangfuseTraces()
    assert client.client == mock_langfuse

def test_list_traces(traces_client, mock_langfuse):
    mock_result = MagicMock()
    mock_trace = MagicMock()
    mock_trace.id = "trace-1"
    mock_trace.name = "test-workflow"
    mock_trace.timestamp = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_trace.total_cost = 0.05
    mock_trace.latency = 1.5
    
    mock_result.data = [mock_trace]
    mock_langfuse.api.trace.list.return_value = mock_result
    
    results = traces_client.list_traces(limit=10, workflow="test-workflow", since="2023-01-01T00:00:00")
    
    mock_langfuse.api.trace.list.assert_called_once_with(
        limit=10,
        page=1,
        name="test-workflow",
        from_timestamp=datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
    )
    
    assert len(results) == 1
    assert results[0]["id"] == "trace-1"
    assert results[0]["name"] == "test-workflow"
    assert results[0]["cost"] == 0.05
    assert results[0]["latency"] == 1.5

def test_show_trace_basic(traces_client, mock_langfuse):
    mock_trace = MagicMock()
    mock_trace.id = "trace-1"
    mock_trace.name = "test-workflow"
    mock_trace.timestamp = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_trace.total_cost = 0.05
    mock_trace.latency = 1.5
    mock_trace.metadata = {"key": "value"}
    
    mock_obs = MagicMock()
    mock_obs.name = "step-1"
    mock_obs.level = "DEFAULT"
    mock_obs.start_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_obs.calculated_total_cost = 0.01
    mock_obs.latency = 0.5
    mock_obs.parent_observation_id = None
    mock_obs.status_message = None
    
    mock_trace.observations = [mock_obs]
    mock_langfuse.api.trace.get.return_value = mock_trace
    
    result = traces_client.show_trace("trace-1")
    
    mock_langfuse.api.trace.get.assert_called_once_with("trace-1")
    assert result["id"] == "trace-1"
    assert len(result["observations"]) == 1
    assert result["observations"][0]["name"] == "step-1"
    assert result["observations"][0]["level"] == "DEFAULT"

def test_show_trace_analyze(traces_client, mock_langfuse):
    mock_trace = MagicMock()
    mock_trace.id = "trace-1"
    mock_trace.name = "test-workflow"
    
    mock_obs1 = MagicMock()
    mock_obs1.name = "tool-1"
    mock_obs1.parent_observation_id = "parent-1"
    mock_obs1.level = "DEFAULT"
    mock_obs1.start_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
    
    mock_obs2 = MagicMock()
    mock_obs2.name = "step-2"
    mock_obs2.level = "ERROR"
    mock_obs2.status_message = "Test error"
    mock_obs2.output = "Error output"
    mock_obs2.start_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_obs2.parent_observation_id = None
    
    mock_trace.observations = [mock_obs1, mock_obs2]
    mock_langfuse.api.trace.get.return_value = mock_trace
    
    result = traces_client.show_trace("trace-1", analyze=True)
    
    assert "tool_counts" in result
    assert result["tool_counts"]["tool-1"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "step-2"
    assert result["errors"][0]["level"] == "ERROR"

def test_cost_per_session(traces_client, mock_langfuse):
    mock_result = MagicMock()
    
    t1 = MagicMock()
    t1.id = "tr-1"
    t1.session_id = "sess-1"
    t1.total_cost = 0.5
    t1.name = "wf-1"
    
    t2 = MagicMock()
    t2.id = "tr-2"
    t2.session_id = "sess-1"
    t2.total_cost = 0.2
    t2.name = "wf-2"
    
    t3 = MagicMock()
    t3.id = "tr-3"
    t3.session_id = "sess-2"
    t3.total_cost = 0.1
    t3.name = "wf-1"
    
    mock_result.data = [t1, t2, t3]
    mock_langfuse.api.trace.list.return_value = mock_result
    
    results = traces_client.cost_per_session()
    
    assert len(results) == 2
    assert results[0]["session_id"] == "sess-1"
    assert results[0]["total_cost"] == 0.7
    assert len(results[0]["trace_ids"]) == 2
    assert results[1]["session_id"] == "sess-2"
    assert results[1]["total_cost"] == 0.1

def test_list_session_traces(traces_client, mock_langfuse):
    mock_result = MagicMock()
    
    t1 = MagicMock()
    t1.id = "tr-1"
    t1.timestamp = datetime(2023, 1, 2, tzinfo=timezone.utc)
    t1.name = "wf-1"
    
    t2 = MagicMock()
    t2.id = "tr-2"
    t2.timestamp = datetime(2023, 1, 1, tzinfo=timezone.utc)
    t2.name = "wf-2"
    
    mock_result.data = [t1, t2]
    mock_langfuse.api.trace.list.return_value = mock_result
    
    results = traces_client.list_session_traces("sess-1")
    
    mock_langfuse.api.trace.list.assert_called_once_with(limit=100, page=1, session_id="sess-1")
    # T2 is older, should be first
    assert results[0]["id"] == "tr-2"
    assert results[1]["id"] == "tr-1"

def test_list_failures(traces_client, mock_langfuse):
    mock_list_result = MagicMock()
    
    t1 = MagicMock()
    t1.id = "tr-1"
    t1.name = "wf-1"
    t1.timestamp = datetime(2023, 1, 1, tzinfo=timezone.utc)
    mock_list_result.data = [t1]
    mock_langfuse.api.trace.list.return_value = mock_list_result
    
    mock_trace = MagicMock()
    mock_trace.id = "tr-1"
    mock_obs1 = MagicMock()
    mock_obs1.level = "ERROR"
    mock_obs1.name = "failed-step"
    mock_trace.observations = [mock_obs1]
    
    mock_langfuse.api.trace.get.return_value = mock_trace
    
    results = traces_client.list_failures()
    
    assert len(results) == 1
    assert results[0]["id"] == "tr-1"
    assert len(results[0]["errors"]) == 1
    assert results[0]["errors"][0]["level"] == "ERROR"

@pytest.mark.asyncio
async def test_traces_list_tool():
    with patch("cai.log.traces._TRACES.list_traces") as mock_list:
        mock_list.return_value = [
            {"id": "tr-1", "name": "wf-1", "timestamp": "2023-01-01T00:00:00", "cost": 0.5, "latency": 1.0}
        ]
        res = await traces_list()
        assert "tr-1" in res
        assert "0.5000" in res

@pytest.mark.asyncio
async def test_traces_show_tool_basic():
    with patch("cai.log.traces._TRACES.show_trace") as mock_show:
        mock_show.return_value = {
            "id": "tr-1", "name": "wf-1", "timestamp": "2023", "cost": 0.5, "latency": 1.0,
            "observations": [
                {"name": "step-1", "level": "DEFAULT", "cost": None, "latency": None, "parent_id": None}
            ]
        }
        res = await traces_show("tr-1")
        assert "tr-1" in res
        assert "step-1" in res
        mock_show.assert_called_once_with("tr-1", full=False, analyze=False)

@pytest.mark.asyncio
async def test_traces_failures_tool():
    with patch("cai.log.traces._TRACES.list_failures") as mock_fail:
        mock_fail.return_value = [
            {
                "id": "tr-1", "name": "wf-1", "timestamp": "2023",
                "errors": [{"name": "step-1", "level": "ERROR", "status_message": "test msg", "output": None}]
            }
        ]
        res = await traces_failures()
        assert "test msg" in res
        assert "tr-1" in res

@pytest.mark.asyncio
async def test_traces_session_cost_tool():
    with patch("cai.log.traces._TRACES.cost_per_session") as mock_cost:
        mock_cost.return_value = [
            {"session_id": "sess-1", "total_cost": 1.25, "trace_count": 3, "workflows": ["wf-1"]}
        ]
        res = await traces_session_cost()
        assert "sess-1" in res
        assert "1.2500" in res

@pytest.mark.asyncio
async def test_traces_session_tool():
    with patch("cai.log.traces._TRACES.list_session_traces") as mock_sess:
        mock_sess.return_value = [
            {"id": "tr-1", "name": "wf-1", "timestamp": "2023", "cost": 0.5, "latency": 1.0}
        ]
        res = await traces_session("sess-1")
        assert "tr-1" in res
        assert "0.5000" in res

def test_helpers():
    class DummyObs:
        pass
        
    obs = DummyObs()
    obs.level = "ERROR"
    assert _has_error_level(obs) is True
    
    obs.level = "DEFAULT"
    assert _has_error_level(obs) is False
    
    assert _sort_key(DummyObs()) == datetime.min.replace(tzinfo=timezone.utc)
    
    obs.start_time = datetime(2023, 1, 1, tzinfo=timezone.utc)
    assert _sort_key(obs) == obs.start_time
