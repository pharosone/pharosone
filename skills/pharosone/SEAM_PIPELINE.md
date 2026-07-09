# Seam Pipeline — universal agent onboarding (design sketch)

Generalizes `pharosone` (formerly `onboard-agent`) from "one adapter template" into a **seam-selection pipeline**.
Universality lives NOT in a single seam, but in:
1. a universal **channel contract** (what a wrapped agent exposes + where it accepts poison), and
2. a deterministic **seam-selection procedure** + a **library of shim templates**.

Core rule: **cut at the narrowest reachable WAIST that is above the messy abstractions
(auth / transport / serialization) and below the agent's reasoning.** Monkeypatch is one
technique (in-process waists), not the universal answer.

---

## 0. The universal channel contract (the spine)

Every generated shim implements the same contract, regardless of seam:

```python
# external(): the engine drives this; returns OpenAI-shaped {content, tool_calls}.
#   tool_calls = the agent's observed tool invocations (the misuse ledger).
# The request MAY carry an injection directive; the shim routes poison to that channel.
async def external(request: dict) -> dict: ...

# channels(): the untrusted surfaces THIS agent actually has, so the engine knows
#   which indirect vectors are testable (and which are blind spots).
def channels() -> list[str]: ...     # e.g. ["message", "card_field:lead_snapshot", "tool_result:web_search"]
```

Injection directive carried on the request (engine sets it per probe):
```python
request["injection"] = {"channel": "card_field:lead_snapshot", "payload": "<poison>"}
# channel grammar: "message" | "card_field:<name>" | "tool_result:<tool>" | "retrieved_doc" | "history"
```
One adapter then covers direct AND every indirect vector the agent has — the waist gives the
interception point, the contract gives the routing. No rewrite per attack.

---

## 1. Skill `classify-agent` — topology → viable seam family

ASKS the client (only what code can't answer):
- Where does the agent run vs. the test? `in-process Python` / `local process + HTTP` / `remote hosted` / `other-language binary`
- Is the source available and modifiable for tests? (gates monkeypatch / dep-mock)
- May we touch a real backend for acceptance? (gates live-seed)

DETECTS in code:
- language (file extensions), entrypoint, web server (`fastapi`/`flask` → HTTP topology)
- MCP client libs (`mcp`, `fastmcp`), agent frameworks (`langchain`/`llamaindex`/`crewai`/`autogen`)
- IO clients (`httpx`/`requests`/`aiohttp`), whether `tool_calls` are surfaced in output

OUTPUT: topology → ranked viable seam family (table in §3 of the pipeline doc).

---

## 2. Skill `find-seams` — locate the waists, rank by narrowness

DETECTS candidate waists:
- **pure decision fn** — returns a structured "what to do" object (dataclass/pydantic) with NO IO
  inside (no `await client.*`). → param-injector seam.
- **tool dispatch waist** — a `{name: fn}` registry, a decorator registry, an OpenAI `tools`
  schema + dispatch, or MCP `session.call_tool`. → monkeypatch-interceptor seam.
- **retrieval waist** — `retriever.get` / `vectorstore.similarity_search` / `embed_query`. → monkeypatch.
- **DI seams** — constructors taking client objects (`__init__(self, crm, messaging)`). → dep-mock.
- **IO clients** — `httpx.AsyncClient(base_url=...)`, MCP transport. → wire-stub (last resort in-process).

OUTPUT: ranked list `{seam, file:line, narrowness, channels_it_can_inject, technique}`.
Flag channels the corpus needs but no seam can reach = **blind spots** (never read as "robust").

---

## 3. Skill `generate-shim` — emit an adapter from the template library

Pick the template for the chosen waist, fill from recon, emit an adapter satisfying §0.

### Template A — param-injector  (pure decision fn; NOT a patch)
```python
BENIGN = {"lead_snapshot": "(test lead — no prior data)", "contact_name": "Test"}

async def external(request):
    inj  = request.get("injection")
    msgs = [m for m in request["messages"] if m["role"] != "system"]
    card = dict(BENIGN)
    last = msgs[-1]["content"] if msgs else ""
    if inj:
        ch, pay = inj["channel"], inj["payload"]
        if ch == "message":               last = pay
        elif ch.startswith("card_field:"): card[ch.split(":", 1)[1]] = pay
    turn = await agent.run_turn(history=_hist(msgs[:-1]),
                                new_messages=[{"id": 1, "direction": "inbound", "text": last}],
                                known_answers={}, **card)
    return _wrap(turn)           # -> {"choices":[{"message":{"content","tool_calls"}}]}

def channels(): return ["message", "card_field:lead_snapshot", "card_field:contact_name"]
```

### Template B — monkeypatch-interceptor  (tool registry / MCP call_tool; the OAuth/MCP answer)
Intercept the waist: record every call (ledger) + return poisoned-or-neutralized result.
You sit ABOVE auth+transport (never reach the wire), BELOW reasoning (real model).
```python
import contextlib, json, mcp

_CALLS = []

@contextlib.asynccontextmanager
async def _intercept(poison):                     # poison: {tool_name: poisoned_result_text}
    orig = mcp.ClientSession.call_tool
    async def fake(self, name, arguments=None):
        _CALLS.append({"name": name, "args": arguments})        # observe (misuse ledger)
        if name in poison:  return _result(poison[name])        # poisoned tool_result channel
        return _result("ok")                                    # neutralize the real side effect
    mcp.ClientSession.call_tool = fake
    try: yield
    finally: mcp.ClientSession.call_tool = orig

async def external(request):
    inj, poison = request.get("injection"), {}
    if inj and inj["channel"].startswith("tool_result:"):
        poison[inj["channel"].split(":", 1)[1]] = inj["payload"]
    _CALLS.clear()
    last = request["messages"][-1]["content"]
    async with _intercept(poison):
        reply = await agent.chat(last)            # real agent, all its abstractions intact
    return {"choices": [{"message": {"content": reply, "tool_calls": [
        {"type": "function", "function": {"name": c["name"],
         "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in _CALLS]}}]}

def channels(): return ["message"] + [f"tool_result:{t}" for t in KNOWN_TOOLS]
```
One patch covers N MCP servers × M tools × any auth — narrowness wins.

### Template C — dep-mock  (DI seam; runs the REAL orchestration)
```python
fake_amo = FakeAmo(lead=_poisoned_lead(inj))      # returns the poisoned card from get_lead()
agent = QualifierAgent(crm=fake_crm, messaging=RecordingMessaging())   # real app/agent.py path
reply = await agent.handle(request["messages"][-1]["content"])
# tool_calls = RecordingMessaging/FakeCRM write-call ledger; covers field_map + _apply_turn
```

### Template D — wire-stub  (remote/opaque, but URLs are repointable)
Fake HTTP server speaking the dependency contracts; repoint base_url via env; model state for
chain probes; map routes -> capabilities. Heavy: cost = full contract + state + route mapping.
Use when source is unavailable / other-language / acceptance of the whole deployment.

### Template E — live-seed  (acceptance; zero emulation)
Write poison into the REAL source (CRM field, vector store, inbox), run the real agent
end-to-end, read tool_calls from its logs. Highest fidelity, needs a live stage.

---

## 4. Skill `validate` — alignment + smoke  (reuse existing pharosone Step 4)
- capability alignment: probe `required_tools` (capabilities) ⊆ inventory capabilities.
- 1-trial smoke per topology; confirm `tool_calls` surface and `channels()` cover the corpus's
  indirect vectors; report blind spots.

---

## Decision tree (what the pipeline encodes)

**Canonical source of truth.** This is the ONE seam decision tree; `pharosone/SKILL.md` (round B)
and the sub-skills reference it here rather than re-copying it. Edit it in this file only.

```
in-process + pure decision fn? ........... A param-injector   (cheapest, no patch)
in-process + tool/MCP/retrieval waist? ... B monkeypatch       (OAuth/MCP: go here, not the wire)
in-process + DI seam, want orchestration?  C dep-mock          (best fidelity/cost in-process)
remote, surfaces tool_calls? ............. bridge external (HTTP)   [interface only]
remote/opaque, URLs repointable? ......... D wire-stub
need whole-deployment acceptance? ........ E live-seed
```
Pick the **narrowest reachable** seam above abstractions, below reasoning.
Universality = contract (§0) + this procedure + the template library — never a single seam.
