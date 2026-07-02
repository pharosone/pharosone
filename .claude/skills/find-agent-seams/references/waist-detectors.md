# Waist detectors & narrowness rubric

How to recognize each waist in code, and how to score narrowness. One interception point should
cover as many downstream concerns as possible while keeping the agent's reasoning real.

## Narrowness rubric (lower = narrower = preferred)

1. **Decision-fn** — inputs are plain function params; you inject by passing a value. No patch, no IO.
2. **Dispatch / MCP / retrieval** — one function mediates ALL tool/data access; one patch covers
   every tool and every backend, above auth+transport, below reasoning.
3. **DI constructor** — you replace whole client objects; runs the real orchestration but you must
   implement the client interfaces (methods, not wire).
4. **IO client / wire** — below the waist; you must speak each external contract + model state +
   map routes→capabilities. Cost grows with #integrations and auth complexity.
5. **Live source** — no emulation; highest fidelity, needs a live stage.

Pick the **narrowest reachable** given topology + `source_modifiable`. Other-language or
remote-opaque agents can't use 1–3 from Python → fall to 4/5 or the HTTP `external` interface.

## Detect patterns

### 1. Pure decision function (→ param_inject)
- A method/function returning a structured result (`@dataclass`, pydantic `BaseModel`, a TypedDict
  of "what to do") whose body has **no** `await client.*` / `requests.*` / file IO.
- The caller (orchestration) fetches data and passes it in as args; the side effects happen
  elsewhere (a separate `apply`/`_apply_turn`). That separation IS the seam.
- Grep: functions taking context params (`history`, `snapshot`, `context`, `messages`) and
  returning an actions object; a sibling `apply`/`execute`/`commit` that performs IO.

### 2a. Tool dispatch registry (→ monkeypatch)
- A dict `{name: fn}` or `TOOLS = {...}`; a decorator registry (`@tool`, `@register`,
  `self._tools[name] = ...`); an OpenAI `tools=[...]` schema + a dispatcher that switches on
  `tool_call.function.name`.
- Patch point: the single dispatch function (e.g. `_dispatch(name, args)` / `execute_tool`).

### 2b. MCP (→ monkeypatch)
- `from mcp ...`, `ClientSession`, `stdio_client`/`sse_client`, `await session.call_tool(name, args)`.
- Patch `ClientSession.call_tool` (or the agent's thin wrapper around it). One patch intercepts
  every MCP server the agent connects to — below OAuth/transport, above reasoning.

### 2c. Retrieval (→ monkeypatch)
- `retriever.get_relevant_documents` / `.aget...`, `vectorstore.similarity_search`, `embed_query`,
  a `retrieve(query)` helper. Patch it to return poisoned-or-benign docs (channel `retrieved_doc`).

### 3. DI constructor (→ dep_mock)
- `def __init__(self, crm, messaging, ...)` / clients injected, or a settings-built container.
- Construct the agent with fake clients: a `FakeX` whose read methods return poisoned data and
  whose write methods record calls (the misuse ledger) without performing side effects.

### 4. IO client / wire (→ wire_stub)
- `httpx.AsyncClient(base_url=...)` with a repointable base URL (env/config); MCP transport URL.
- Stand up a fake server speaking the contract; repoint base_url; model state for chain probes;
  map HTTP routes/payloads → capabilities. Verify base_url is actually overridable (env/config,
  not hardcoded; no TLS pinning) before recommending.

### 5. Live source (→ live_seed)
- Only when acceptance of the whole deployment is wanted and a safe stage exists. Write poison
  into the real CRM field / vector store / inbox; run the real agent; read tool_calls from logs.

## Channel reachability per waist

Always state which untrusted channels the chosen waist can inject (`message`, `card_field:<f>`,
`tool_result:<tool>`, `retrieved_doc`, `history`). A channel the corpus needs but the waist can't
reach is a **blind spot** → report it; do not let it read as robust.
