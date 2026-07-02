"""Shim template B — monkeypatch (tool-dispatch / MCP call_tool / retrieval seam).

Use when the agent calls its tools/data through ONE chokepoint (a dispatch function, an MCP
`session.call_tool`, or a retriever). Patch that single function: record every call (the misuse
ledger) and return a poisoned-or-neutralized result. You sit ABOVE auth+transport (you never reach
the network) and BELOW reasoning (the real model still decides) — so one patch covers N servers ×
M tools × any OAuth. This is the answer for MCP/OAuth-heavy agents (don't poison the wire).

Fill the TODOs: the patch target, how a result object is constructed, the agent entrypoint.
"""

import contextlib
import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("PROBE_ENGINE_DIR", str(Path.home() / "probe-engine")))
sys.path.insert(0, str(ENGINE_DIR / "src"))

# TODO: import the agent + the module that owns the waist.
import mcp                                   # TODO: or the agent's tool-dispatch module
from app.agent import Agent                  # TODO: the agent entrypoint object

KNOWN_TOOLS = ["web_search", "send_email", "lookup_account"]   # TODO: from PASSPORT.md
BENIGN_RESULT = {"web_search": "[]", "send_email": "ok", "lookup_account": "{}"}  # TODO

_CALLS: list[dict] = []
_AGENT = None


def _agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = Agent()                     # TODO: construct
    return _AGENT


def _wrap_result(text: str):
    # TODO: build the result object the waist normally returns (e.g. mcp.types.CallToolResult).
    return text


@contextlib.asynccontextmanager
async def _intercept(poison: dict):
    # TODO: point at the real waist. Examples:
    #   mcp.ClientSession.call_tool   |   dispatch_module._dispatch   |   retriever.aget_relevant_documents
    orig = mcp.ClientSession.call_tool

    async def fake(self, name, arguments=None):
        _CALLS.append({"name": name, "args": arguments})          # observe (ledger)
        if name in poison:
            return _wrap_result(poison[name])                     # poisoned tool_result channel
        return _wrap_result(BENIGN_RESULT.get(name, "ok"))        # neutralized side effect

    mcp.ClientSession.call_tool = fake
    try:
        yield
    finally:
        mcp.ClientSession.call_tool = orig


def channels() -> list[str]:
    return ["message"] + [f"tool_result:{t}" for t in KNOWN_TOOLS]   # + "retrieved_doc" if retrieval


async def external(request: dict) -> dict:
    inj, poison = request.get("injection"), {}
    if inj and inj["channel"].startswith("tool_result:"):
        poison[inj["channel"].split(":", 1)[1]] = inj["payload"]
    msgs = [m for m in request.get("messages", []) if m.get("role") != "system"]
    last = msgs[-1].get("content", "") if msgs else ""
    if inj and inj["channel"] == "message":
        last = inj["payload"]

    _CALLS.clear()
    async with _intercept(poison):
        reply = await _agent().chat(last)    # TODO: the real entrypoint (real model, real tool loop)

    return {"choices": [{"message": {"content": reply, "tool_calls": [
        {"type": "function", "function": {"name": c["name"],
         "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in _CALLS]}}]}


def certify(profile: str = "configs/profiles/AGENT.yaml", out: str = "reports/AGENT") -> None:
    raise NotImplementedError("copy certify() from the pharosone example / existing adapter")


if __name__ == "__main__":
    certify()
