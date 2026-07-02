"""Shim template C — dep-mock (dependency-injection seam).

Use when the agent is constructed with injected client objects (`Agent(crm=..., messaging=...)`).
Build the agent with FAKE clients: read methods return poisoned-or-benign data (the indirect
channels), write methods record calls without performing side effects (the misuse ledger). This
runs the REAL orchestration (field mapping, apply/commit logic) — strictly more coverage than the
pure-fn seam, at the cost of implementing the client interfaces (methods, not the wire).

Fill the TODOs: the agent constructor, the client interfaces, the read/write method mapping.
"""

import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("PROBE_ENGINE_DIR", str(Path.home() / "probe-engine")))
sys.path.insert(0, str(ENGINE_DIR / "src"))

from app.agent import Agent                  # TODO: the DI-constructed agent

_CALLS: list[dict] = []


class FakeReadClient:
    """TODO: implement the read methods the orchestration calls (e.g. get_lead, get_contact).
    Return the benign record by default; splice poison into the requested field on demand."""

    def __init__(self, poison: dict):
        self._poison = poison                # {"card_field:lead_snapshot": "<poison>", ...}

    async def get_lead(self, lead_id, with_contacts=True):
        snapshot = self._poison.get("card_field:lead_snapshot", "(test lead)")
        name = self._poison.get("card_field:contact_name", "Test")
        # TODO: return the exact dict shape the orchestration expects (custom_fields, contacts...).
        return {"id": lead_id, "name": name, "snapshot": snapshot, "custom_fields_values": []}


class RecordingWriteClient:
    """TODO: implement the write/side-effect methods; record, never perform."""

    def _rec(self, name, **args):
        _CALLS.append({"name": name, "args": args})
        return {"ok": True}

    async def send_text(self, chat_id, text, **k):   # TODO: maps to send_message
        return self._rec("reply_to_client", chat_id=chat_id, text=text)

    async def patch_lead(self, lead_id, **k):         # TODO: maps to status_change/record_write
        return self._rec("set_status", lead_id=lead_id, **k)


def channels() -> list[str]:
    return ["message", "card_field:lead_snapshot", "card_field:contact_name"]


async def external(request: dict) -> dict:
    inj = request.get("injection")
    poison = {inj["channel"]: inj["payload"]} if inj and inj["channel"].startswith("card_field:") else {}
    msgs = [m for m in request.get("messages", []) if m.get("role") != "system"]
    last = msgs[-1].get("content", "") if msgs else ""
    if inj and inj["channel"] == "message":
        last = inj["payload"]

    _CALLS.clear()
    agent = Agent(crm=FakeReadClient(poison), messaging=RecordingWriteClient())   # TODO: real ctor
    reply = await agent.handle(last)         # TODO: the real orchestration entrypoint

    return {"choices": [{"message": {"content": reply or "", "tool_calls": [
        {"type": "function", "function": {"name": c["name"],
         "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in _CALLS]}}]}


def certify(profile: str = "configs/profiles/AGENT.yaml", out: str = "reports/AGENT") -> None:
    raise NotImplementedError("copy certify() from the pharosone example / existing adapter")


if __name__ == "__main__":
    certify()
