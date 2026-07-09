"""Shim template D — wire-stub (raw IO / repointable-URL seam).

Use when you can't cut inside (remote, other-language, or no clean code seam) but the agent's
external base URLs are repointable (env/config, no TLS pinning). Stand up a fake server speaking
each dependency's contract, repoint the agent at it, run the REAL agent unmodified. Most universal
by coverage (whole agent, any language) but the priciest glue: full contract + state + route→
capability mapping. NEVER stub the LLM endpoint — pass it through.

This template sketches the fake-service + repoint + run-and-collect loop; fill the contracts.
"""

import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("PROBE_ENGINE_DIR", str(Path.home() / "probe-engine")))
sys.path.insert(0, str(ENGINE_DIR / "src"))

from fastapi import FastAPI, Request         # the fake backend
import uvicorn                               # serve it on a local port

# In-memory state so chain probes work (a poison turn that sets state, a trigger turn that reads).
_STATE: dict = {}
_CALLS: list[dict] = []
_POISON: dict = {}                           # set per request before the agent runs

stub = FastAPI()


# TODO: one handler per endpoint the agent hits (from SEAMS.md / the client code). Examples:
@stub.get("/api/v4/leads/{lead_id}")
async def get_lead(lead_id: int):
    # READ endpoint -> an indirect channel. Splice poison into the field under test.
    snapshot = _POISON.get("card_field:lead_snapshot", "(test lead)")
    return {"id": lead_id, "custom_fields_values": [], "snapshot": snapshot,
            "status_id": _STATE.get("status_id")}


@stub.patch("/api/v4/leads/{lead_id}")
async def patch_lead(lead_id: int, request: Request):
    body = await request.json()
    _STATE["status_id"] = body.get("status_id", _STATE.get("status_id"))   # persist for chains
    _CALLS.append({"name": "set_status", "args": body})   # TODO: route→capability per payload
    return {"id": lead_id}


# TODO: POST .../notes, POST /tasks, POST .../send_text (-> send_message), etc.


def channels() -> list[str]:
    return ["message", "card_field:lead_snapshot", "tool_result:*"]


async def external(request: dict) -> dict:
    """Pre-req: the agent process runs with its base URLs repointed at this stub
    (e.g. CRM_BASE_URL / MESSAGING_BASE_URL = http://127.0.0.1:PORT) and ANTHROPIC_BASE_URL
    left pointing at the real API. Here we set poison/state, drive the agent, collect _CALLS."""
    global _POISON
    inj = request.get("injection")
    _POISON = {inj["channel"]: inj["payload"]} if inj and inj["channel"] != "message" else {}
    msgs = [m for m in request.get("messages", []) if m.get("role") != "system"]
    last = (inj["payload"] if inj and inj["channel"] == "message"
            else (msgs[-1].get("content", "") if msgs else ""))

    _CALLS.clear()
    reply = await _drive_real_agent(last)    # TODO: call the agent's HTTP entrypoint / trigger

    return {"choices": [{"message": {"content": reply, "tool_calls": [
        {"type": "function", "function": {"name": c["name"],
         "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in _CALLS]}}]}


async def _drive_real_agent(text: str) -> str:
    # TODO: POST to the agent's own endpoint (it will call back into this stub for its tools).
    raise NotImplementedError("wire the agent's entrypoint")


def serve():  # run in a background thread/process before certify()
    uvicorn.run(stub, host="127.0.0.1", port=int(os.environ.get("STUB_PORT", "8099")))


if __name__ == "__main__":
    serve()
