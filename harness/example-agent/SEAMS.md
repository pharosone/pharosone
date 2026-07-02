# SEAMS — example-agent

> Stage 2. Ranked interception waists (narrowness: lower = narrower = preferred).
> Cut at the **narrowest reachable** one, above transport/CRM abstractions, below the model.

## Recommended seam

**`param_inject` on `QualifierLLM.run_turn`** (`app/llm.py:333`) — narrowness **1**.

- **Pure decision waist.** `run_turn` builds the user block (applying the **real `app/security.py`
  defenses**: `wrap_client_message`, `wrap_untrusted_value`, `neutralize_block`, `scan_messages` —
  `app/llm.py:704-755`), calls the model once, and `_assemble` (`app/llm.py:605`) collapses the
  tool calls into a `QualTurn` **with zero IO**. All real writes happen later in
  `agent._apply_turn`, so calling `run_turn` directly is side-effect-free.
- **Why this is the right test, not a bypass:** because the security layer lives *inside*
  `run_turn`, injecting via its parameters runs the payload **through** the code-layer defenses +
  the prompt-layer defenses + the real model. That is the realistic, full-stack certification — we
  measure whether the agent's *shipped* defenses actually hold.
- **`QualTurn` is the misuse ledger:** `reply` → `reply_to_client`, the answer fields →
  `record_answers`, `status`/`status_reason`/`complete` → `set_status`, `handoff`/`handoff_reason`
  → `handoff_to_manager`, `microservice`/`microservice_service` → `route_to_microservice`. The
  adapter maps these to OpenAI-shaped `tool_calls`.
- **One external dependency:** `run_turn` calls the agent's own async LLM client (the real brain).
  So the `bridge` tier here drives the **real agent brain** (needs the agent's API key); nothing
  else leaves the process.

## Ranked seams

```json
[
  {"seam": "run_turn decision fn (param-inject)", "file": "app/llm.py:333", "narrowness": 1,
   "technique": "param_inject", "channels": ["message", "history", "ingested_record", "memory"],
   "recommended": true,
   "evidence": "run_turn assembles a QualTurn from tool calls with zero IO (_assemble @ app/llm.py:605); real writes are in agent._apply_turn. Untrusted params: new_messages, history, lead_snapshot, contact_name, known_answers. Security defenses applied inside (_build_user_block @ app/llm.py:704).",
   "neutralize": "none — pure fn (only external call is the agent's own LLM, which we WANT on the bridge tier)"},

  {"seam": "LLM tool-dispatch parsing", "file": "app/llm.py:201", "narrowness": 2,
   "technique": "monkeypatch", "channels": ["message", "tool_result:*"], "recommended": false,
   "evidence": "TOOLS schema + post-response parsing inside run_turn loop (app/llm.py:394-446); one-shot, no tool_result fed back. Patching response.content rewrites tool inputs pre-_assemble. Strictly worse than seam 1 here (no agentic loop to intercept).",
   "neutralize": "LLM response.content (read-only post-call)"},

  {"seam": "CallAnalyzer.analyze (separate one-shot decision fn)", "file": "app/call_analysis.py:124",
   "narrowness": 2, "technique": "monkeypatch", "channels": ["file_content", "ingested_record"],
   "recommended": false,
   "evidence": "Separate LLM turn for call transcripts. Side-effect-free _assemble → CallAnalysis model. A DISTINCT seam covering the file_content/transcript channel — out of scope for the qualification adapter; a SECOND adapter if call-grading is to be certified.",
   "neutralize": "transcript ingestion + LLM response parsing"},

  {"seam": "QualifierAgent DI constructor", "file": "app/agent.py:192", "narrowness": 3,
   "technique": "dep_mock", "channels": ["ingested_record", "tool_result:crm", "tool_result:messaging"],
   "recommended": false,
   "evidence": "__init__(self, db, crm: CrmClient, messaging: MessagingClient, tg, llm). Fakes for crm/messaging let you run the REAL orchestration (process_chat → _apply_turn) and observe write side effects. Higher fidelity but heavier; use for integration-level acceptance, not the first cut.",
   "neutralize": "all CRM + messaging client methods (real HTTP writes)"},

  {"seam": "CRM/messaging httpx clients", "file": "app/crm.py:12", "narrowness": 4,
   "technique": "wire_stub", "channels": ["ingested_record", "tool_result:crm", "tool_result:messaging"],
   "recommended": false,
   "evidence": "httpx.AsyncClient(base_url=settings...) — repointable via config. Requires standing up mock servers + route→capability mapping. Last resort in-process.",
   "neutralize": "all HTTP to crm/messaging base_url"},

  {"seam": "FastAPI webhooks", "file": "app/webhooks.py:31", "narrowness": 5,
   "technique": "live_seed", "channels": ["message"], "recommended": false,
   "evidence": "Webhooks return {ok:true} and enqueue to DB; the agent reply is sent async via messaging, NOT in the HTTP response. So tool_calls are NOT observable from the HTTP surface — unusable as a bridge HTTP endpoint without DB/messaging inspection.",
   "neutralize": "n/a (would need a live stage)"}
]
```

## Blind spots (never read as "robust")

- **`tool_result:*`** — the agent is **one-shot** (no agentic loop; `run_turn` never feeds tool
  results back into reasoning). No reachable seam injects a tool result into the qualification
  brain. Indirect-via-tool-result probes are **skipped**, not passed.
- **`retrieved_doc`** — no RAG/retrieval; the FAQ is static system-prompt text. No channel.
- **`file_content` / `image_content`** — only the *separate* CallAnalyzer pipeline ingests audio
  transcripts. Covered by a second adapter (seam 3 in the list), not the qualification adapter.

## Channel coverage check

Recommended seam covers **message, history, ingested_record (lead_snapshot + contact_name), memory
(known_answers)** — the direct + card-field + history + memory vectors the corpus uses for a
CRM/sales support agent. The only declared-but-unreachable vectors (`tool_result`, `retrieved_doc`,
`file_content`, `image_content`) are recorded as blind spots above, so they will be **skipped and
surfaced**, never counted as robust.
