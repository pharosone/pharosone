# Integration snippets — curl, Python, TypeScript, Go

All snippets read the base URL and API key from the environment:

```bash
export PHAROSONE_BASE_URL="https://pharos.example.com"
export PHAROSONE_API_KEY="…"   # minted in the cabinet under API Keys; set by the user, never echoed
```

## Raw HTTP (curl)

### 1. upsert-agent

```bash
curl -sS -X POST "$PHAROSONE_BASE_URL/api/v1/upsert-agent" \
  -H "Authorization: Bearer $PHAROSONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "support-bot",
    "description": "Tier-1 support assistant for the ACME storefront.",
    "goal": "Resolve the customer'\''s issue or hand off to a human with full context."
  }'
```

### 2. send-message (per-turn; here: a tool call)

```bash
curl -sS -X POST "$PHAROSONE_BASE_URL/api/v1/send-message" \
  -H "Authorization: Bearer $PHAROSONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "support-bot",
    "session_id": "chat-4211",
    "message_id": "chat-4211-0007",
    "role": "tool",
    "text": "",
    "tool_call": {
      "name": "search_kb",
      "label": "Search knowledge base",
      "status": "ok",
      "args_preview": "query='\''refund policy'\''",
      "result_preview": "3 articles: Refunds, Returns, Chargebacks"
    }
  }'
# → 202 {"status":"received","dialog_id":"dlg_…","message_index":7,"created":true,
#         "flagged":false,"fast_scan":"ok"}
```

### 3. send-dialog (snapshot)

```bash
curl -sS -X POST "$PHAROSONE_BASE_URL/api/v1/send-dialog" \
  -H "Authorization: Bearer $PHAROSONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "support-bot",
    "session_id": "chat-4211",
    "messages": [
      {"role": "user", "text": "Hi, how do refunds work?"},
      {"role": "tool", "text": "", "tool_call": {"name": "search_kb", "label": "Search knowledge base", "status": "ok", "result_preview": "3 articles"}},
      {"role": "bot", "text": "Refunds are processed within 5 business days."}
    ]
  }'
# → 202 {"status":"received","dialog_id":"dlg_…","flagged":false,"fast_scan":"ok"}
```

### 4. dialog-analysis (detailed verdict on demand)

```bash
# Synchronous: the server computes the deep analysis if needed and blocks until
# it's done (worst case ~75s) — give curl a generous --max-time.
curl -sS --max-time 90 -X POST "$PHAROSONE_BASE_URL/api/v1/dialog-analysis" \
  -H "Authorization: Bearer $PHAROSONE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "support-bot", "session_id": "chat-4211"}'
# or select by the dialog_id from a send response (one selector, not both):
#   -d '{"dialog_id": "dlg_…"}'
# → 200 {"dialog_id":"dlg_…","status":"flagged","analysis_status":"done",
#         "flagged":true,"flag":{…,"mappings":[…]},"effectiveness":{…}}
```

## Python — `pharosone-dialogs`

Zero runtime deps (stdlib only), Python ≥3.10. PyPI publish is pending — until
then install from source:
`pip install "git+https://github.com/dmitry-shirokov/vector-kya#subdirectory=sdk/python"`
(after publish: `pip install pharosone-dialogs`).

```python
from pharosone_dialogs import Message, PharosOne, PharosOneError, ToolCall

client = PharosOne()  # falls back to PHAROSONE_BASE_URL / PHAROSONE_API_KEY
# or explicit: PharosOne(base_url="https://pharos.example.com", api_key=os.environ["PHAROSONE_API_KEY"])

client.upsert_agent(
    agent_id="support-bot",
    description="Tier-1 support assistant for the ACME storefront.",
    goal="Resolve the customer's issue or hand off to a human with full context.",
)

# per-turn streaming, inside the bot loop
client.send_message(agent_id="support-bot", session_id=chat_id,
                    message_id=f"{chat_id}-{seq}", role="user", text=user_text)
client.send_message(agent_id="support-bot", session_id=chat_id,
                    message_id=f"{chat_id}-{seq + 1}", role="tool", text="",
                    tool_call=ToolCall(name="search_kb", label="Search knowledge base",
                                       status="ok", args_preview="query='refund policy'",
                                       result_preview="3 articles"))
res = client.send_message(agent_id="support-bot", session_id=chat_id,
                          message_id=f"{chat_id}-{seq + 2}", role="bot", text=reply_text)

# Every send response carries the fast verdict for the dialog so far.
# fast_scan == "failed" means the scan didn't run — NO verdict, never treat as clean.
if res["fast_scan"] == "ok" and res["flagged"]:
    # Synchronous: blocks while the server computes the deep analysis (~75s worst
    # case); get_analysis uses a >=90s per-call timeout by default (timeout= overrides).
    verdict = client.get_analysis(agent_id="support-bot", session_id=chat_id)
    # or: client.get_analysis(dialog_id=res["dialog_id"]) — one selector form, not both
    if verdict["analysis_status"] == "done" and verdict["flagged"]:
        print(verdict["flag"]["title"], verdict["flag"]["severity"], verdict["effectiveness"])

# snapshot alternative (batch/backfill)
client.send_dialog(agent_id="support-bot", session_id=chat_id, messages=[
    Message(role="user", text="Hi, how do refunds work?"),
    Message(role="bot", text="Refunds are processed within 5 business days."),
])
```

Errors raise `PharosOneError(status, detail)`.

## TypeScript — `@pharosone/dialogs`

Zero runtime deps (global `fetch`), ESM. Registry publish is pending and npm
cannot install a git-repo subdirectory — until then, clone the repo and
`npm pack` the SDK, then install the tarball (see `sdk/typescript/README.md`):

```bash
git clone https://github.com/dmitry-shirokov/vector-kya
(cd vector-kya/sdk/typescript && npm install && npm run build && npm pack)
npm install ./vector-kya/sdk/typescript/pharosone-dialogs-*.tgz
# after registry publish: npm install @pharosone/dialogs
```

The TS surface is camelCase; the client maps it to the snake_case wire keys.
The constructor takes a single options object.

```ts
import { PharosOne } from "@pharosone/dialogs";

const client = new PharosOne(); // falls back to PHAROSONE_BASE_URL / PHAROSONE_API_KEY
// or explicit: new PharosOne({ baseUrl: "https://pharos.example.com", apiKey: process.env.PHAROSONE_API_KEY })

await client.upsertAgent({
  agentId: "support-bot",
  description: "Tier-1 support assistant for the ACME storefront.",
  goal: "Resolve the customer's issue or hand off to a human with full context.",
});

// per-turn streaming, inside the bot loop
await client.sendMessage({
  agentId: "support-bot", sessionId: chatId,
  messageId: `${chatId}-${seq}`, role: "user", text: userText,
});
await client.sendMessage({
  agentId: "support-bot", sessionId: chatId,
  messageId: `${chatId}-${seq + 1}`, role: "tool", text: "",
  toolCall: { name: "search_kb", label: "Search knowledge base", status: "ok",
              argsPreview: "query='refund policy'", resultPreview: "3 articles" },
});
const res = await client.sendMessage({
  agentId: "support-bot", sessionId: chatId,
  messageId: `${chatId}-${seq + 2}`, role: "bot", text: replyText,
});

// Every send result carries the fast verdict for the dialog so far.
// fastScan === "failed" means the scan didn't run — NO verdict, never treat as clean.
if (res.fastScan === "ok" && res.flagged) {
  // Synchronous: blocks while the server computes the deep analysis (~75s worst case);
  // getAnalysis defaults this call's timeout to >=90s ({ timeoutMs } overrides).
  const verdict = await client.getAnalysis({ agentId: "support-bot", sessionId: chatId });
  // or: client.getAnalysis({ dialogId: res.dialogId }) — one selector form, not both
  if (verdict.analysisStatus === "done" && verdict.flagged) {
    console.log(verdict.flag?.title, verdict.flag?.severity, verdict.effectiveness);
  }
}

// snapshot alternative (batch/backfill)
await client.sendDialog({
  agentId: "support-bot", sessionId: chatId,
  messages: [
    { role: "user", text: "Hi, how do refunds work?" },
    { role: "bot", text: "Refunds are processed within 5 business days." },
  ],
});
```

## Go — `sdk/go` (package `pharosone`)

Zero deps beyond `net/http`. Install: `go get github.com/dmitry-shirokov/vector-kya/sdk/go`.

```go
import pharosone "github.com/dmitry-shirokov/vector-kya/sdk/go"

client := pharosone.New(os.Getenv("PHAROSONE_BASE_URL"), os.Getenv("PHAROSONE_API_KEY"))

_, err := client.UpsertAgent(ctx, pharosone.UpsertAgentParams{
    AgentID:     "support-bot",
    Description: pharosone.Ptr("Tier-1 support assistant for the ACME storefront."),
    Goal:        pharosone.Ptr("Resolve the customer's issue or hand off to a human with full context."),
})

// per-turn streaming, inside the bot loop
_, err = client.SendMessage(ctx, pharosone.SendMessageParams{
    AgentID: "support-bot", SessionID: chatID,
    MessageID: pharosone.Ptr(fmt.Sprintf("%s-%04d", chatID, seq)),
    Role:      pharosone.RoleTool, Text: "",
    ToolCall: &pharosone.ToolCall{
        Name: "search_kb", Label: "Search knowledge base", Status: pharosone.ToolStatusOK,
        ArgsPreview:   pharosone.Ptr("query='refund policy'"),
        ResultPreview: pharosone.Ptr("3 articles"),
    },
})
res, err := client.SendMessage(ctx, pharosone.SendMessageParams{
    AgentID: "support-bot", SessionID: chatID,
    MessageID: pharosone.Ptr(fmt.Sprintf("%s-%04d", chatID, seq+1)),
    Role:      pharosone.RoleBot, Text: replyText,
})

// Every send result carries the fast verdict for the dialog so far.
// FastScanFailed means the scan didn't run — NO verdict, never treat as clean.
if err == nil && res.FastScan == pharosone.FastScanOK && res.Flagged {
    // Synchronous: blocks while the server computes the deep analysis (~75s worst
    // case); GetAnalysis stretches the per-request timeout to >=90s automatically.
    verdict, err := client.GetAnalysis(ctx, pharosone.GetAnalysisParams{
        AgentID: pharosone.Ptr("support-bot"), SessionID: pharosone.Ptr(chatID),
    })
    // or: pharosone.GetAnalysisParams{DialogID: pharosone.Ptr(res.DialogID)} — one form, not both
    if err == nil && verdict.AnalysisStatus == "done" && verdict.Flagged {
        fmt.Println(verdict.Flag.Title, verdict.Flag.Severity, verdict.Effectiveness)
    }
}

// snapshot alternative (batch/backfill)
_, err = client.SendDialog(ctx, pharosone.SendDialogParams{
    AgentID: "support-bot", SessionID: chatID,
    Messages: []pharosone.Message{
        {Role: pharosone.RoleUser, Text: "Hi, how do refunds work?"},
        {Role: pharosone.RoleBot, Text: "Refunds are processed within 5 business days."},
    },
})
```

API errors are `*pharosone.APIError{Status, Detail}`.
