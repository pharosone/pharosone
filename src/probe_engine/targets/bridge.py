"""Bridge target: drive a REAL external client agent, turn-by-turn (spec §3.1 `bridge`).

The agent under test is a black box described by a single async callable:

    external(request: dict) -> response: dict        # OpenAI chat-completions shape

The engine owns the conversation loop (so single_turn / chain / adaptive scenarios all run
against the real agent), records the agent's *reported* tool calls into the sandbox store
(so the standard oracle detects tool misuse / leaks / state change), and captures the
turn-by-turn transcript into Evidence.

Two ways to obtain `external`:
  - `http_endpoint_agent(...)` — point at an OpenAI-compatible HTTP endpoint (no code).
  - a hand-written async adapter wrapping a framework agent (LangChain / CrewAI / MCP / raw),
    passed via `run_corpus(..., external=...)`.

Honesty / observability limits over bridge (the engine only sees what the agent reports):
  - ALL tool-based oracles (`tool_called` / `state_changed` / `secret_leaked`) require the
    agent to *surface* its tool calls as OpenAI `tool_calls`. An agent that performs an action
    WITHOUT reporting the call is scored as robust (a false PASS). The `contains` oracle reads
    only the assistant text and is unaffected.
  - `state_changed` / `secret_leaked` additionally need the run to declare a tool inventory
    with `dangerous` / `leaks_if_path_contains` flags, so a reported call can be interpreted;
    without the relevant flag that oracle can never fire (the engine raises at build time for
    bridge so this is surfaced, not silently passed).
  - `secret_leaked` inspects tool-call ARGUMENTS only (via `leaks_if_path_contains`), never the
    assistant text. A secret pasted into the agent's reply must be caught by a `contains` oracle.
"""

import json
from typing import Any, Awaitable, Callable

from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageSystem,
    ChatMessageUser,
    ModelOutput,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.util import store

from probe_engine.domain.probe import Probe
from probe_engine.domain.run import RunConfig, ToolSpec
from probe_engine.sandbox.state import (
    adjudicate_call,
    advance_stage,
    empty_outcome,
    mark,
    parse_arg,
    record_tool_call,
    seed_from_meta,
)
from probe_engine.scoring.oracle import STORE_OUTCOME

ExternalAgent = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


# ----- request / response shaping --------------------------------------------------------


def to_openai_messages(messages: list) -> list[dict]:
    """Serialize Inspect ChatMessages to plain OpenAI-style {role, content} dicts."""
    out: list[dict] = []
    for m in messages:
        role = getattr(m, "role", "user")
        text = getattr(m, "text", None)
        if text is None:
            text = str(getattr(m, "content", ""))
        out.append({"role": role, "content": text})
    return out


def openai_tools(inventory: list[ToolSpec] | None) -> list[dict]:
    """Advertise the declared inventory as OpenAI function tools (single required `args`,
    mirroring the model-tier sandbox tools, so OpenAI-strict endpoints accept the schema)."""
    out: list[dict] = []
    for s in inventory or []:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description or f"{s.name} tool",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "args": {
                                "type": "string",
                                "description": "JSON or text arguments (recipient, amount, path, ...)",
                            }
                        },
                        "required": ["args"],
                        "additionalProperties": False,
                    },
                },
            }
        )
    return out


def extract_message(response: dict[str, Any]) -> tuple[str, list[dict]]:
    """Pull (assistant_text, tool_calls) from an OpenAI-style response, tolerantly.

    Handles `choices[0].message`, a top-level `message`, and flat `content/output/
    response/text` shapes. Each returned tool call is `{"name": str, "arguments": str|dict}`.
    """
    if not isinstance(response, dict):
        return (str(response), [])

    msg: dict | None = None
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        cand = choices[0].get("message")
        if isinstance(cand, dict):
            msg = cand
    if msg is None and isinstance(response.get("message"), dict):
        msg = response["message"]
    if msg is None:
        for key in ("output", "response", "content", "text"):
            val = response.get(key)
            if isinstance(val, str):
                return (val, [])
            if isinstance(val, dict) and ("content" in val or "tool_calls" in val):
                msg = val
                break
    if msg is None:
        return ("", [])

    content = msg.get("content") or ""
    if isinstance(content, list):  # content-parts form (tolerate non-str parts / plain strings)
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text", "")))
        content = " ".join(parts).strip()
    elif not isinstance(content, str):
        content = str(content)

    calls: list[dict] = []
    for c in msg.get("tool_calls") or []:
        if not isinstance(c, dict):
            continue
        fn = c.get("function") if isinstance(c.get("function"), dict) else c
        name = fn.get("name")
        if name:
            calls.append({"name": name, "arguments": fn.get("arguments")})
    return (str(content), calls)


def record_external_tool_calls(
    outcome: dict, tool_calls: list[dict], inventory: list[ToolSpec] | None
) -> dict:
    """Fold the agent's reported tool calls into the sandbox outcome, using the declared
    inventory to interpret danger / leaks (so the standard oracle can read end-state)."""
    spec_by_name = {s.name: s for s in (inventory or [])}
    for call in tool_calls:
        name = call.get("name")
        if not name:
            continue
        raw = call.get("arguments")
        # ensure_ascii=False so non-ASCII (e.g. Cyrillic) tool-arg values round-trip and a
        # tool_arg canary/needle in the same encoding still matches (parity with _serialized_args).
        args_str = raw if isinstance(raw, str) else json.dumps(raw, default=str, ensure_ascii=False)
        spec = spec_by_name.get(name)
        caps = spec.effective_capabilities() if spec else [name]
        record_tool_call(outcome, name, {"args": args_str}, capabilities=caps)
        if spec:
            # lifecycle: a reported call carrying a stage_arg advances the state machine.
            if spec.stage_arg:
                new_stage = parse_arg(raw, spec.stage_arg)
                if new_stage is not None:
                    advance_stage(outcome, new_stage)
            # authorization: adjudicate the reported call against the seeded ownership backend.
            decision = adjudicate_call(
                outcome, authz_action=spec.authz_action, resource_arg=spec.resource_arg, raw_args=raw
            )
            if decision == "deny":
                # modeled backend BLOCKED it: the action did not take effect — no state change / leak
                # is recorded (enforcement, not willingness). The attempt is still in tool_calls.
                continue
            if spec.dangerous:
                mark(outcome, state_changed=True)
            if spec.leaks_if_path_contains and spec.leaks_if_path_contains in (args_str or ""):
                mark(outcome, secret_leaked=True)
    return outcome


def _assistant_render(text: str, calls: list[dict]) -> str:
    """Transcript-friendly assistant line: text plus a tool-call summary if any."""
    if calls:
        summary = ", ".join(c["name"] for c in calls if c.get("name"))
        return (f"{text}\n[tool_calls] {summary}".strip()) if summary else (text or "(no content)")
    return text or "(no content)"


# ----- HTTP endpoint adapter (feature 1) -------------------------------------------------


def http_endpoint_agent(
    endpoint: str,
    *,
    api_key: str | None = None,
    model: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
    transport: Any = None,
) -> ExternalAgent:
    """Build an `external` callable that POSTs an OpenAI-style chat-completions request to a
    real agent's HTTP endpoint. `api_key` is sent as a Bearer token (never stored). `model`
    is added to the payload if set. `transport` is for tests (httpx.MockTransport)."""
    import httpx

    base_headers = {"Content-Type": "application/json"}
    if api_key:
        base_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        base_headers.update(headers)

    async def external(request: dict[str, Any]) -> dict[str, Any]:
        payload = dict(request)
        if model:
            payload["model"] = model
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if transport is not None:
            client_kwargs["transport"] = transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(endpoint, json=payload, headers=base_headers)
            resp.raise_for_status()
            return resp.json()

    return external


# ----- engine-driven turn loop over the external agent -----------------------------------


def _first_user_text(messages: list) -> str:
    for m in messages:
        if getattr(m, "role", "") == "user":
            text = getattr(m, "text", None)
            return text if text is not None else str(getattr(m, "content", ""))
    return ""


def bridge_target_solver(
    probe: Probe, run_config: RunConfig | None, external: ExternalAgent
) -> Solver:
    """Drive the probe's turns (single_turn / chain) against the external agent."""
    inventory = run_config.tool_inventory if run_config else None
    sys_prompt = run_config.target.system_prompt if run_config else None

    @solver
    def _solver() -> Solver:
        async def solve(state: TaskState, generate: Generate) -> TaskState:
            meta = state.metadata or {}
            turns = meta.get("turns") or [{"prompts": [_first_user_text(state.messages)]}]
            tools = openai_tools(inventory)
            outcome = seed_from_meta(empty_outcome(), meta)
            store().set(STORE_OUTCOME, outcome)

            state.messages = [ChatMessageSystem(content=sys_prompt)] if sys_prompt else []
            last_text = ""
            for turn in turns:
                content = " ".join(turn.get("prompts") or [])
                channel_payloads = turn.get("channel_payloads")
                injection: dict[str, str] | None = None
                injections: list[dict[str, str]] = []
                if channel_payloads:
                    # MULTI-CHANNEL (Option B): one DISTINCT variation per channel, delivered to all
                    # of them at once. Conversation channels ride inline in the turn text; every
                    # ingestion channel becomes a separate routed injection for the adapter.
                    for ch, payload in channel_payloads.items():
                        if ch in ("message", "history"):
                            content = (content + "\n[external content] " + payload).strip()
                        else:
                            injections.append({"channel": ch, "payload": payload})
                else:
                    poison = turn.get("poison")
                    channel = turn.get("channel", "message")
                    if poison and channel in ("message", "history"):
                        # conversation channel: poison rides inside the turn text (tier-agnostic default)
                        content = (content + "\n[external content] " + poison).strip()
                    elif poison:
                        # ingestion channel: hand the poison to the adapter to route into the real
                        # surface (card field / tool result / retrieved doc) — NOT into the message.
                        injection = {"channel": channel, "payload": poison}
                content = content or "(continue)"
                state.messages.append(ChatMessageUser(content=content))

                request: dict[str, Any] = {"messages": to_openai_messages(state.messages)}
                if injection:
                    request["injection"] = injection
                if injections:
                    # plural: the adapter routes each into its real surface. A single ingestion
                    # channel still arrives here as a one-element list (multi_channel path).
                    request["injections"] = injections
                if tools:
                    request["tools"] = tools
                response = await external(request)
                text, calls = extract_message(response)
                record_external_tool_calls(outcome, calls, inventory)
                store().set(STORE_OUTCOME, outcome)
                last_text = text
                state.messages.append(ChatMessageAssistant(content=_assistant_render(text, calls)))

            state.output = ModelOutput.from_content("bridge", last_text)
            return state

        return solve

    return _solver()
