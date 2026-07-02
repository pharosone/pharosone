"""Shim template E — live-seed (acceptance; zero emulation).

Use ONLY for whole-deployment acceptance against a SAFE staging environment. Nothing is mocked:
you write the poison into the REAL data source (a CRM field, a vector-store doc, an inbox), run
the real agent end-to-end, then read its actions back from logs/audit. Highest fidelity, but you
own real-world setup, teardown, and side-effect safety.

DANGER: this performs REAL effects. Only against a disposable staging tenant, never production.
Confirm with the user before running. Fill the seed/run/collect/teardown TODOs.
"""

import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("PROBE_ENGINE_DIR", str(Path.home() / "probe-engine")))
sys.path.insert(0, str(ENGINE_DIR / "src"))


def channels() -> list[str]:
    # Real source can carry any indirect channel; declare the ones you actually seed.
    return ["message", "card_field:lead_snapshot", "retrieved_doc"]


async def _seed(channel: str, payload: str) -> dict:
    # TODO: write poison into the real staging source; return a handle for teardown.
    #   card_field:* -> patch a staging CRM lead field
    #   retrieved_doc -> upsert a poisoned doc into the staging vector store
    raise NotImplementedError("seed the staging source")


async def _run_agent(message: str) -> str:
    # TODO: send `message` through the real agent (its real endpoint / trigger) and await completion.
    raise NotImplementedError("drive the real agent")


async def _collect_actions() -> list[dict]:
    # TODO: read the agent's actions from logs/audit trail since the run started; map to tool_calls.
    raise NotImplementedError("collect actions from logs")


async def _teardown(handle: dict) -> None:
    # TODO: remove the seeded poison / reset the staging lead.
    raise NotImplementedError("teardown")


async def external(request: dict) -> dict:
    inj = request.get("injection")
    msgs = [m for m in request.get("messages", []) if m.get("role") != "system"]
    message = (inj["payload"] if inj and inj["channel"] == "message"
               else (msgs[-1].get("content", "") if msgs else ""))

    handle = {}
    if inj and inj["channel"] != "message":
        handle = await _seed(inj["channel"], inj["payload"])
    try:
        reply = await _run_agent(message)
        calls = await _collect_actions()
    finally:
        if handle:
            await _teardown(handle)

    return {"choices": [{"message": {"content": reply, "tool_calls": [
        {"type": "function", "function": {"name": c["name"],
         "arguments": json.dumps(c.get("args", {}), ensure_ascii=False)}} for c in calls]}}]}


def certify(profile: str = "configs/profiles/AGENT.yaml", out: str = "reports/AGENT") -> None:
    raise NotImplementedError("copy certify(); confirm staging safety with the user first")


if __name__ == "__main__":
    certify()
