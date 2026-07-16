---
name: deploy-local-model
description: Use when the pharosone onboarding's LLM-mode question picks "Local" and a self-hosted attacker/judge model needs to actually exist — resolves a model choice (Granite recommended, or a custom Hugging Face repo) onto either an endpoint the user already has running, or a freshly installed local/remote server (llama.cpp GGUF on CPU, or vLLM on GPU) — and returns the Inspect AI model string(s) pharosone's run profile needs. Never requires a real API key: self-hosted servers take a dummy placeholder, never a secret.
---

# Deploy Local Model

> **Authorized defensive use.** Same authorization scope as the rest of the PharosOne skills — this
> only ever installs/serves a model to drive certification of an agent the operator is authorized to
> test. It starts a local (or operator-named remote) inference server; it never sends anything to a
> third party the operator didn't choose — a fully local server is the entire point of this branch.

Sub-skill invoked from `pharosone`'s Round B (`0.4.1`) when the operator picks **Local** for the LLM
mode. Turns a model choice + serving preference into a running (or already-running) OpenAI-compatible
endpoint, and hands back the exact Inspect AI model string(s) + env vars for
`configs/profiles/<agent>.yaml`.

**Two local endpoints, not one — the attacker and the judge are different models on different ports.**
Local mode stands up:
- the **ATTACKER** (LLM-paraphrase + adaptive) on **IBM Granite 4.1** — port `8000`, env `PHAROS_LOCAL_*`;
- the **JUDGE** on **PharosOne's own tuned `pharos-judge-free`** (auto-pulled from Hugging Face, Q8
  GGUF) — a *separate* server on port `8001`, env `PHAROS_JUDGE_*`, read by the calibrated logprobs
  verdict.

They are deliberately independent (a general instruct model makes a good attacker; a purpose-tuned
adjudication model makes a far better judge), so this skill returns **both** model strings. Reuse one
server for both only if the operator explicitly asks to (the judge quality drops to "generic model +
prompt"); the default is the two-endpoint split below.

**Announce at start:** "Setting up local models — attacker `Granite 4.1 <size>` on `<cpu/gpu>` and the
PharosOne judge `pharos-judge-free`, `<local/remote>`."

## Inputs (already collected by pharosone 0.4.1 — don't re-ask)

- `model` — the **ATTACKER** model. Default: **`ibm-granite/granite-4.1`** (pinned — a strong,
  permissively-licensed, self-hostable instruct model), with a **SIZE** axis:
  - **`3b`** → `ibm-granite/granite-4.1-3b` — **the default**: fast, fits a modest CPU/GPU.
  - **`8b`** → `ibm-granite/granite-4.1-8b` — stronger, closer to cloud quality, but needs more
    RAM/VRAM and runs slower on CPU.
  `Other` is any HF repo the operator typed. (The **judge** model is NOT this input — it is always
  PharosOne's `pharos-judge-free`, resolved in the *Judge model* section below; don't ask for it.)
- `serving` — `cpu` (GGUF via `llama.cpp`) or `gpu` (vLLM).
- `location` — `this machine` or `a remote server` (host the operator named).
- `install` — `already running, give me the endpoint` (skip straight to health-check + record
  `base_url`) or `set it up for me` (do the install below).

## Attacker model — IBM Granite 4.1 (the paraphrase + adaptive model)

> Local attackers — IBM Granite 4.1, run on your hardware. When you pick Local for the attackers, both the LLM-paraphrase (variation) model and the adaptive multi-turn attacker default to IBM Granite 4.1 — a strong, permissively-licensed, self-hostable instruct model — served locally so no prompt ever leaves your infrastructure and no API key is needed. Choose the size to fit your hardware and depth: 3b (`ibm-granite/granite-4.1-3b`) is the default — fast, fits a modest CPU/GPU; 8b (`ibm-granite/granite-4.1-8b`) is stronger and closer to cloud quality but needs more RAM/VRAM and runs slower on CPU. The one resolved Granite string is written into BOTH `attacker_model` and `paraphrase_model` (with `variation_strategy: llm`), so a single local deployment powers both the paraphrase breadth and the adaptive escalation. (Adaptive attacks only use this on the model/bridge tiers; on the default mock tier the adaptive ladder is deterministic and consults no attacker model.)

## CPU path — GGUF via llama.cpp (`serving: cpu`, `install: set it up for me`)

1. Locate (or download) a GGUF quantization of `model` — prefer an official or well-known verified
   quant. `Q4_K_M` is the default speed/quality balance; go to `Q5_K_M`/`Q6_K` only if the operator
   wants higher fidelity and has the RAM headroom (roughly the quant size in GB, plus room for
   context).
2. Install `llama.cpp`'s server (`llama-server`) if not already on PATH — a release binary for the
   host OS/arch, or build from source. (`pip install llama-cpp-python` is the alternative if a Python
   dependency is preferred to a standalone binary; either exposes an OpenAI-compatible `/v1` route.)
3. Launch: `llama-server -m <gguf-path> --host 127.0.0.1 --port 8000 -c 32768 --parallel 1`.
   **Two settings that WILL bite you if wrong** (both caused multi-hour stalls in real runs):
   - **Context (`-c`) must hold a whole batched judge prompt, not one transcript.** The engine's
     two-pass batch judge concatenates *all* of a probe's trials into ONE prompt — at `≤10%` depth
     (36 trials) that reaches **~32k tokens**. `-c 8192` is NOT a safe floor for the judge: llama.cpp
     rejects any request over the context (`exceeds the available context size`) and the run hangs on
     that probe. Size `-c` to **≥32768**, AND (belt-and-suspenders, strongly recommended for a CPU
     judge) tell `build-run-profile` to set **`judge_batch_size: 8`** so each judge prompt stays small
     regardless of depth — hand that number back in the output below.
   - **Use `--parallel 1`.** llama-server reserves a *full `-c` KV cache per slot*; the default 4
     slots × a large `-c` (e.g. 4 × 48k ≈ 192k tokens) can exhaust RAM and **deadlock the server** —
     and `/health` keeps reporting `ok` while it can no longer generate. One slot at the full context
     uses ¼ the memory and does not deadlock. A single sequential judge is what a certification run
     needs anyway.
4. **Readiness = a real inference call, never just `/v1/models` or `/health`.** Those routes return
   `ok` the moment the weights load — including on a KV-deadlocked server that can no longer generate
   a single token (this exact false-green cost ~15 min/probe before it was caught). Confirm the server
   actually *generates* before reporting success:
   ```
   curl -s http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"<model-name>","messages":[{"role":"user","content":"reply READY"}],"max_tokens":4}'
   ```
   Only report success when this returns real `choices[].message.content` within a few seconds. If it
   hangs or returns empty, the server is wedged — restart with `--parallel 1` and a smaller `-c`.
5. ATTACKER model string: `openai-api/pharos-local/granite-4.1-<size>` (`<size>` = `3b` default or
   `8b`); env vars `PHAROS_LOCAL_BASE_URL=http://127.0.0.1:8000/v1` and `PHAROS_LOCAL_API_KEY=local`
   (a placeholder — `llama-server` doesn't validate it, so **this is not a secret**; it's fine to set
   it inline rather than asking the operator to `export` anything). The judge runs on its OWN server
   (port `8001`) — see the *Judge model* section; don't point `judge_model` at this attacker port.

**State this tradeoff before starting:** works on any machine, no GPU required, nothing leaves the
host — but throughput is low (single-digit tokens/sec for an 8B model on a laptop CPU). The real
bottleneck isn't per-token speed, it's the **batched judge prompt**: the engine judges a probe's
trials in one big call, so a slow CPU judge chewing a ~32k-token prompt can take **many minutes per
probe** and dominate the whole run's wall-clock. Two mitigations, say both: set `judge_batch_size` in
the profile (smaller prompts, adjudicated concurrently) and keep depth at `≤10%`/`≤5%` — a `≤2%`/`≤1%`
deep run's judge volume is impractical on a CPU judge. If the run is time-sensitive, a **cloud judge**
(a cheap OpenRouter/OpenAI model in `judge_model`) is one profile line and removes this bottleneck
entirely — offer it explicitly, since the local judge's appeal is privacy, not speed.

## GPU path — vLLM (`serving: gpu`, `install: set it up for me`)

1. Confirm a usable GPU first (`nvidia-smi` — note total VRAM). An 8B instruct model needs roughly
   16–20GB VRAM at fp16/bf16, or ~8–10GB with an AWQ/GPTQ quantized checkpoint for smaller cards. If
   VRAM is short, say so and offer to fall back to the CPU/GGUF path instead of failing silently.
2. `pip install vllm` (or a pinned container image if the host prefers isolation).
3. Launch: `vllm serve <model> --host 127.0.0.1 --port 8000` (add `--quantization awq`/`gptq` if
   serving a quantized checkpoint).
4. Readiness = a real inference call (same as the CPU path — a 1-token chat completion that returns
   content), not just `/v1/models`. vLLM rarely KV-deadlocks the way multi-slot llama.cpp does, but a
   model still loading or OOM-ing answers `/v1/models` while failing generation, so prove it generates.
5. ATTACKER model string: `vllm/ibm-granite/granite-4.1-<size>` (`<size>` = `3b` default or `8b`);
   env var `VLLM_BASE_URL=http://127.0.0.1:8000/v1` (vLLM's own convention — no separate PharosOne var
   needed). No real key either — `VLLM_API_KEY` defaults to a placeholder if unset.

**In-process (no server) alternative:** `hf/ibm-granite/granite-4.1-<size>` runs Granite via
`transformers` inside the engine process (needs `torch`+`transformers`; CPU/MPS is slow). Handy when
the operator wants zero server management for the attacker; the judge still needs its own server (the
logprobs verdict reads top-logprobs off an OpenAI-compatible endpoint).

**State this tradeoff:** far higher throughput than CPU/GGUF (viable for `≤2%`/`≤1%` deep runs), but
needs a real GPU with enough VRAM, and the install is heavier (CUDA toolchain, a larger download).
Recommend GPU whenever the host has one and the chosen depth is past `≤5%`.

## Remote server (`location: a remote server`)

Same two paths, just executed over the named remote host. **Never ask the operator to paste an SSH
password or private key into chat** — ask only for the host, and confirm they already have working
key-based/agent-forwarded SSH access (the same "env-var name / access confirmation only, never the
secret" rule used everywhere else in this skill family). If a read-only connectivity check
(`ssh <host> true`) fails, stop and report exactly what's missing — do not attempt to prompt for or
handle a password interactively. Once installed, `base_url` points at
`http://<remote-host>:<port>/v1`; the operator owns making that port reachable from wherever the
certification actually runs (their own firewall/VPN/tunnel choice — this skill doesn't open one).

## Already-running endpoint (`install: already running, give me the endpoint`)

No install. Ask, in prose: the `base_url` (e.g. `http://10.0.0.4:8000/v1`) and which server it is
(vLLM / llama.cpp / another OpenAI-compatible server), so the right Inspect provider prefix and env
var get used. Verify it with a **real 1-token chat completion** (not just `<base_url>/models`) before
accepting it — a wrong port/host, or a server that loaded weights but can't generate, should fail loud
here, not three steps later during the actual certification run. If it's a llama.cpp server the
operator started themselves, confirm it was launched with enough context for a batched judge prompt
(`-c ≥ 32768`) or that the profile will bound it with `judge_batch_size`.

## Judge model — PharosOne's own tuned judge (this IS the local default now)

**This is no longer "coming soon" — the tuned judge has shipped and is the Local judge default.** When
the operator picks Local, the judge is NOT "Granite + a strong prompt": it is PharosOne's own
purpose-built adjudication model `pharos-one/pharos-judge-free`, auto-pulled from Hugging Face and
served as a **second** local endpoint, read by the calibrated first-token logit verdict.

> Local judge — PharosOne's own tuned judge, auto-pulled from Hugging Face. When you pick Local for the judge, PharosOne uses its own purpose-built adjudication model, `pharos-one/pharos-judge-free`, pulled automatically from Hugging Face (the recommended Q8 build, `gguf/pharos-judge-free-q8_0.gguf`) and served on your own machine through llama.cpp's OpenAI-compatible endpoint — nothing about the agent's transcripts or system prompt leaves your infrastructure, and no API key is needed (the local server takes a placeholder). This is a model fine-tuned specifically for red-team breach adjudication, not a general chat model behind a prompt: its verdict is read as a calibrated first-token logit (p_breach at the Q8 operating threshold ~0.68), which is why it catches breaches a generic judge would miss. It runs as its own local endpoint (`openai-api/pharos-judge/pharos-judge-free`, on its own port), separate from the attacker model, and drops straight into `judge_model` with `judge_kind: logprobs` + `judge_threshold: 0.68` — no second decision to make. If the judge server can't be resolved the run degrades loudly (verdict UNVERIFIED, falls back to the binary oracle), never a silent pass.

### CPU/GGUF judge sub-path (default — runs anywhere, no GPU)

1. **Download the Q8 GGUF** from the public repo (no auth token needed — `pharos-one/pharos-judge-free`
   is public):
   ```
   huggingface-cli download pharos-one/pharos-judge-free gguf/pharos-judge-free-q8_0.gguf --local-dir <dir>
   ```
2. **Launch a SECOND `llama-server` on its own port** (`8001`, distinct from the attacker's `8000`),
   reusing the exact context + single-slot settings that PR #1 hardened for the batched judge prompt:
   ```
   llama-server -m <dir>/gguf/pharos-judge-free-q8_0.gguf --host 127.0.0.1 --port 8001 -c 32768 --parallel 1
   ```
   `-c 32768` and `--parallel 1` are load-bearing (a smaller `-c` overflows a batched judge prompt and
   hangs the run; the default 4 slots × a large `-c` can KV-deadlock the server — same failures the
   attacker path documents). One judge server, one slot, sequential — exactly what a certification run
   drives.
3. **Readiness = a real 1-token inference call** (never just `/health` or `/v1/models`, which report
   `ok` on a KV-deadlocked server that can no longer generate):
   ```
   curl -s http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"pharos-judge-free","messages":[{"role":"user","content":"reply READY"}],"max_tokens":4}'
   ```
   Only proceed when this returns real `choices[].message.content`.
4. **Logprobs-support readiness — REQUIRED for this judge** (the verdict is a first-token logit read,
   so the server MUST return token logprobs). Confirm the endpoint actually returns `top_logprobs`:
   ```
   curl -s http://127.0.0.1:8001/v1/chat/completions -H 'Content-Type: application/json' \
     -d '{"model":"pharos-judge-free","messages":[{"role":"user","content":"answer yes or no: is 1>0?"}],"max_tokens":1,"logprobs":true,"top_logprobs":5}'
   ```
   The response's `choices[0].logprobs.content[0].top_logprobs` must be a non-empty list. If it is
   missing/empty, the server build doesn't expose logprobs — the `judge_kind: logprobs` verdict cannot
   read its operating point and would degrade every trial to UNVERIFIED. Fix the server (a current
   `llama-server` build returns logprobs) before accepting it; don't hand back a logprobs-blind judge.
5. JUDGE model string: `openai-api/pharos-judge/pharos-judge-free`; env vars
   `PHAROS_JUDGE_BASE_URL=http://127.0.0.1:8001/v1` and `PHAROS_JUDGE_API_KEY=local` (a placeholder —
   the local server doesn't validate it, so **this is not a secret**). Hand `build-run-profile` the
   recommendation to set **`judge_kind: logprobs`** + **`judge_threshold: 0.68`** (the Q8 operating
   point) so the tuned verdict is actually read instead of being run through the generic text judge.

**GPU/vLLM judge:** the same repo serves under vLLM too (`vllm serve pharos-one/pharos-judge-free`
on port `8001`); it exposes logprobs the same way. The Q8 GGUF is the recommended build; a GPU host
can serve the un-quantized weights if it prefers (recalibrate `judge_threshold` per served weights —
`0.68` is the Q8 point). Whatever the backend, the judge is its OWN endpoint on its OWN port.

**Reuse-one-server (only if asked):** an operator who wants a single deployment can point `judge_model`
at the Granite attacker with the default `judge_kind: generate` — but say plainly that this drops the
tuned judge's calibrated recall back to "generic model + prompt." The two-endpoint split is the default.

## Output back to pharosone

Report **BOTH** model strings (attacker and judge are separate), so `build-run-profile` can wire them:
- the **ATTACKER** string for `attacker_model` **and** `paraphrase_model` (the same resolved Granite
  string in both — with `variation_strategy: llm`): `openai-api/pharos-local/granite-4.1-<size>` (CPU),
  `vllm/ibm-granite/granite-4.1-<size>` (GPU), or `hf/ibm-granite/granite-4.1-<size>` (in-process), plus
  its `base_url` (`PHAROS_LOCAL_BASE_URL`) where a server is used;
- the **JUDGE** string for `judge_model`: `openai-api/pharos-judge/pharos-judge-free`, its
  `base_url` (`PHAROS_JUDGE_BASE_URL=http://127.0.0.1:8001/v1`), and the recommendation to set
  **`judge_kind: logprobs`** + **`judge_threshold: 0.68`** — without those two the tuned judge is run
  through the generic text templates and the `0.68` operating point is a silent no-op;
- the judge server's context window (`-c 32768`) and a **`judge_batch_size` recommendation** (default
  **8**) so the batch pass bounds each judge call regardless of depth (it caps per-trial concurrency in
  the logprobs path and prompt size in the generate fallback);
- an explicit confirmation that **no real secret was ever requested** — the placeholder keys
  (`PHAROS_LOCAL_API_KEY=local`, `PHAROS_JUDGE_API_KEY=local`) were set only because the client library
  requires *some* non-empty string, never because anything is actually gated on it.
