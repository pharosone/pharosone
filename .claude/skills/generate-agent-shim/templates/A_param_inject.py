"""Shim template A — param-inject (pure decision-function seam).

Use when the agent exposes a PURE decision function: it takes context as parameters and returns a
structured "what to do" object, performing NO side effects itself (those live in a separate apply
step the harness never calls). Injection = passing poison as a parameter. This is NOT a patch and
needs no mocks — the adapter simply stands in for the orchestration that would build those params.

Fill the TODOs from PASSPORT.md / SEAMS.md, then `certify()` runs the corpus.
"""

import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("PROBE_ENGINE_DIR", str(Path.home() / "probe-engine")))
sys.path.insert(0, str(ENGINE_DIR / "src"))

# TODO: import the agent's pure decision layer + its result type.
from app.llm import QualifierLLM            # TODO: the pure decision object
from app.models import QualTurn             # TODO: its return type

# TODO: benign defaults for every non-message input channel (the "clean lead").
BENIGN = {"lead_snapshot": "(qualification test lead — no prior data)", "contact_name": "Test"}

_AGENT = None


def _agent():
    global _AGENT
    if _AGENT is None:
        _AGENT = QualifierLLM()              # TODO: construct
    return _AGENT


def _to_tool_calls(turn: "QualTurn") -> list[dict]:
    """TODO: map the agent's chosen actions to OpenAI tool_calls. Names MUST match the profile."""
    chosen: list[tuple[str, dict]] = []
    if turn.answers_dict():
        chosen.append(("record_answers", turn.answers_dict()))
    if turn.reply:
        chosen.append(("reply_to_client", {"text": turn.reply}))
    chosen.append(("set_status", {"status": turn.status, "reason": turn.status_reason}))
    if turn.handoff:
        chosen.append(("handoff_to_manager", {"reason": turn.handoff_reason}))
    return [{"type": "function", "function": {"name": n, "arguments": json.dumps(a, ensure_ascii=False)}}
            for n, a in chosen]


def channels() -> list[str]:
    # TODO: exactly the channels this seam can inject (from SEAMS.md).
    return ["message", "card_field:lead_snapshot", "card_field:contact_name", "history"]


async def external(request: dict) -> dict:
    inj = request.get("injection")           # {"channel": ..., "payload": ...} or None
    msgs = [m for m in request.get("messages", []) if m.get("role") != "system"]
    card = dict(BENIGN)
    history = [{"id": i + 1, "direction": "inbound" if m["role"] == "user" else "outbound",
                "text": m.get("content", "") or ""} for i, m in enumerate(msgs[:-1])]
    last = msgs[-1].get("content", "") if msgs else ""

    if inj:
        ch, pay = inj["channel"], inj["payload"]
        if ch == "message":
            last = pay
        elif ch.startswith("card_field:"):
            card[ch.split(":", 1)[1]] = pay
        elif ch == "history" and history:
            history.insert(0, {"id": 0, "direction": "inbound", "text": pay})

    turn = await _agent().run_turn(           # TODO: the real entrypoint + its param names
        history=history,
        new_messages=[{"id": len(history) + 1, "direction": "inbound", "text": last}],
        known_answers={},
        **card,
    )
    return {"choices": [{"message": {"content": turn.reply or "", "tool_calls": _to_tool_calls(turn)}}]}


def certify(profile: str = "configs/profiles/AGENT.yaml", out: str = "reports/AGENT") -> None:
    # TODO: same body as your existing certify() — load profile, run_corpus(..., external=external).
    raise NotImplementedError("copy certify() from the pharosone example / existing adapter")


if __name__ == "__main__":
    certify()
