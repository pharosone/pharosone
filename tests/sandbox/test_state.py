import asyncio

from inspect_ai.util import store

from probe_engine.domain.probe import SandboxSpec
from probe_engine.domain.run import ToolSpec
from probe_engine.sandbox.state import empty_outcome, mark, record_tool_call
from probe_engine.sandbox.tools import _dynamic_tool, sandbox_tools
from probe_engine.scoring.oracle import STORE_OUTCOME


def test_empty_outcome_shape():
    o = empty_outcome()
    # the original misuse keys, unchanged for backward compatibility...
    assert o["tool_calls"] == [] and o["secret_leaked"] is False and o["state_changed"] is False
    # ...plus the offline stateful-backend keys (resources/principal/policy/authz/stage).
    assert o["resources"] == {} and o["principal"] is None and o["authz"] == []
    assert o["authz_policy"] == {"default": "deny"} and o["stage"] is None and o["stage_history"] == []


def test_record_tool_call_appends():
    o = record_tool_call(empty_outcome(), "move_file", {"src": "a", "dst": "b"})
    assert o["tool_calls"] == [
        {"name": "move_file", "args": {"src": "a", "dst": "b"}, "capabilities": ["move_file"]}
    ]


def test_record_tool_call_carries_capabilities():
    o = record_tool_call(empty_outcome(), "reply_to_client", {}, capabilities=["send_message"])
    assert o["tool_calls"][0]["capabilities"] == ["send_message"]


def test_mark_sets_flags():
    o = mark(empty_outcome(), secret_leaked=True)
    assert o["secret_leaked"] is True
    assert o["state_changed"] is False


def test_sandbox_tools_built_from_spec():
    tools = sandbox_tools(SandboxSpec())
    names = {getattr(t, "__name__", type(t).__name__) for t in tools}
    assert len(tools) >= 1   # at least one tool object returned


def test_dynamic_sandbox_tool_records_effective_capabilities():
    # the model tier gives the agent real Inspect tools built from the inventory; invoking one must
    # record the spec's effective_capabilities so capability-targeted oracles fire in the model tier.
    store().set(STORE_OUTCOME, empty_outcome())
    td = _dynamic_tool(ToolSpec(name="reply_to_client", capabilities=["send_message"]))
    asyncio.run(td.tool('{"to": "x"}'))
    call = store().get(STORE_OUTCOME)["tool_calls"][-1]
    assert call["name"] == "reply_to_client"
    assert call["capabilities"] == ["send_message"]
