# PharosOne ingest API — wire contract

Three JSON-over-HTTPS endpoints. Base URL = the user's PharosOne instance
(e.g. `https://pharos.example.com`).

## Auth

Every request carries the ingest API key (minted in the cabinet under **API Keys**):

```
Authorization: Bearer <key>
Content-Type: application/json
```

The key is read from an environment variable at runtime; snippets reference the variable by
name (`PHAROSONE_API_KEY` by convention), never the value.

## Idempotency model

- **Dialog identity** — `(org, agent_id, session_id)`. The org comes from the API key; the same
  `agent_id` + `session_id` always addresses the same dialog. Use the bot's own
  conversation/chat id as `session_id`.
- **Message identity** — optional `message_id` (≤200 chars, unique per dialog). On
  `send-message`, a known `message_id` updates the existing row in place instead of appending —
  retries are safe, and late patches (e.g. a tool result) land on the original row. Inside one
  `send-dialog` snapshot a duplicated `message_id` is rejected with 409 `duplicate message_id`.
- **Agent identity** — `agent_id` matches by internal id first, then by name; an unknown value
  auto-creates the agent.

## Message and tool_call shapes

Message (used by both ingest endpoints):

| field | type | notes |
|---|---|---|
| `message_id` | string, optional | idempotent upsert key, ≤200 chars |
| `role` | `user` \| `bot` \| `tool` | `tool` for tool invocations |
| `text` | string | may be empty on tool_call messages |
| `ts` | RFC 3339 timestamp, optional | omitted on insert → arrival time; omitted on update → stored ts kept (late patches don't reorder) |
| `tool_call` | object, optional | see below |

`tool_call`:

```json
{
  "name": "search_kb",
  "label": "Search knowledge base",
  "status": "ok",
  "args_preview": "query='refund policy'",
  "result_preview": "3 articles: Refunds, Returns, Chargebacks"
}
```

`status` ∈ `ok` | `denied` | `error` | `pending`. `args_preview` / `result_preview` are optional
short previews (analysis renders them as `tool: <name> [<status>] args=… result=…`).

Optional `end_user` (both ingest endpoints): `external_id`, `email`, `name`, `ip`, `user_agent`,
`locale`, `timezone`, `referrer` — all optional strings.

## POST /api/v1/upsert-agent — create or update an agent

Idempotent. Resolution: `agent_id` as internal id → as name → create (name = `name` ??
`agent_id`). Omitted optional fields keep their stored values; renaming to a name already taken
by another agent → 409.

Request:

```json
{
  "agent_id": "support-bot",
  "name": "Support Bot",
  "description": "Tier-1 support assistant for the ACME storefront.",
  "goal": "Resolve the customer's issue or hand off to a human with full context."
}
```

Response `200`:

```json
{
  "id": "agt_01h…",
  "name": "Support Bot",
  "description": "Tier-1 support assistant for the ACME storefront.",
  "goal": "Resolve the customer's issue or hand off to a human with full context.",
  "agent_context_json": {},
  "created_at": "2026-01-01T12:00:00Z",
  "updated_at": "2026-01-01T12:00:00Z"
}
```

## POST /api/v1/send-message — stream one message (per-turn)

Appends (or, with a known `message_id`, updates) a single message; creates the dialog on first
call. Request = one message + the dialog key:

```json
{
  "agent_id": "support-bot",
  "session_id": "chat-4211",
  "message_id": "chat-4211-0007",
  "role": "tool",
  "text": "",
  "ts": "2026-01-01T12:00:07Z",
  "tool_call": {
    "name": "search_kb",
    "label": "Search knowledge base",
    "status": "ok",
    "args_preview": "query='refund policy'",
    "result_preview": "3 articles: Refunds, Returns, Chargebacks"
  },
  "end_user": { "external_id": "u-981", "locale": "en-US" }
}
```

Response `202`:

```json
{ "status": "received", "dialog_id": "dlg_01h…", "message_index": 7, "created": true }
```

`message_index` is the 0-based index of the written row; `created: false` means the
`message_id` matched an existing row and it was updated in place.

## POST /api/v1/send-dialog — full snapshot (batch/backfill)

Replaces the dialog's entire message list with `messages` (snapshot semantics — NOT an append).
Same dialog key; messages may carry `message_id` (pass-through, duplicate inside one snapshot →
409).

```json
{
  "agent_id": "support-bot",
  "session_id": "chat-4211",
  "messages": [
    { "role": "user", "text": "Hi, how do refunds work?", "ts": "2026-01-01T12:00:00Z" },
    { "role": "tool", "text": "", "tool_call": { "name": "search_kb", "label": "Search knowledge base", "status": "ok", "args_preview": "query='refund policy'", "result_preview": "3 articles" } },
    { "role": "bot", "text": "Refunds are processed within 5 business days…" }
  ],
  "end_user": { "external_id": "u-981" }
}
```

Response `202`: `{ "status": "received", "dialog_id": "dlg_01h…" }`

## Analysis: analyze-on-idle

Every ingest write (per-turn or snapshot) marks the dialog live and resets its analysis to
pending; the analyzer picks the dialog up once it goes idle. Streaming a long conversation
message-by-message therefore does NOT trigger an analysis per message — the dialog is analyzed
after the conversation pauses, and re-analyzed if new messages arrive later. Tool calls are part
of the analyzed transcript.

## Errors

Errors are JSON problem documents with a human-readable `detail`:

```json
{ "status": 422, "title": "Unprocessable Entity", "detail": "validation failed" }
```

- `401` — missing/invalid API key (check the env var holds an ingest key).
- `409` — conflict: duplicate `message_id` inside one snapshot, or agent rename to a taken name.
- `422` — body violates the contract (lengths, enums, required fields).
