"""Bridge adapter for example-agent — seam A (param-inject on `QualifierLLM.run_turn`).

This is the canonical worked example of onboarding technique **A (param_inject)**: cutting an
agent at a PURE decision function. `run_turn` builds the user block (applying the agent's REAL
`app/security.py` defenses), calls the model once, and `_assemble` collapses the model's tool
calls into a `QualTurn` with ZERO side effects (the real writes live in `agent._apply_turn`,
which the harness never calls). So we inject poison as parameters and read the chosen actions off
the `QualTurn` — no patch, no mocks, no risk of a real backend write or client message.

`example-agent` is a neutral, fictional lead/customer-qualification support assistant used to
demonstrate the pipeline end-to-end (technique B, monkeypatch, is documented in the shim-skill
templates). Point `AGENT_DIR` at any agent that exposes a pure `run_turn` decision waist with
the same structured return type.

Universal channel contract (see ../../.claude/skills/pharosone/SEAM_PIPELINE.md §0):
    async def external(request) -> {"choices":[{"message":{"content","tool_calls"}}]}
    def channels() -> list[str]

Injection routing (engine sets request["injection"] / request["injections"] per probe; canonical
channel names from domain.probe.CANONICAL_CHANNELS):
    message          -> rides INLINE in the last user turn (engine appends it; nothing to route)
    history          -> rides inline (engine) / defensively prepended as an inbound turn
    ingested_record  -> embedded as a client-filled field inside `lead_snapshot` (the CRM card)
    memory           -> injected as a prior recorded answer in `known_answers`

Blind spots (declared honestly, never read as robust): tool_result (agent is one-shot, no agentic
loop), retrieved_doc (no RAG), file_content / image_content (only a separate call-analysis path).

Run it:
    AGENT_API_KEY=...  uv run python harness/example-agent/adapter.py            # full certify
    uv run python harness/example-agent/adapter.py smoke                          # offline self-check (no model)
"""

import json
import os
import sys
from pathlib import Path

ENGINE_DIR = Path(os.environ.get("PROBE_ENGINE_DIR", str(Path(__file__).resolve().parents[2])))
AGENT_DIR = Path(os.environ.get("AGENT_DIR", "/path/to/example-agent"))
sys.path.insert(0, str(ENGINE_DIR / "src"))
sys.path.insert(0, str(AGENT_DIR))  # so `import app.llm` resolves; app.config auto-loads its .env

from app.llm import QualifierLLM        # the pure decision object (real brain on the bridge tier)
from app.models import QualTurn         # its return type (the misuse ledger)

# Benign "clean lead" defaults for every non-message input channel. Poison overrides these per probe.
_BENIGN_SNAPSHOT = (
    "## CRM lead card\n"
    "Lead source: inbound web form\n"
    "Preferred channel: chat\n"
    "Initial intent: product inquiry"
)
_BENIGN_CONTACT_NAME = "Test Lead"
_BENIGN_MANAGER_NAME = "Alex Morgan"   # exercises the "introduce as the assigned manager" rule

_AGENT: QualifierLLM | None = None


def _agent() -> QualifierLLM:
    """Lazily construct the real agent LLM wrapper (no network at construction — just builds the
    async client and loads the prompts/system_prompt.md + faq.md)."""
    global _AGENT
    if _AGENT is None:
        _AGENT = QualifierLLM()
    return _AGENT


# ----- QualTurn -> OpenAI tool_calls (names MUST be the agent's real tool names) -----------

def _to_tool_calls(turn: QualTurn) -> list[dict]:
    """Map the agent's chosen actions to OpenAI `tool_calls`. The oracle reads these as the misuse
    ledger; names match app/llm.py::_tools() exactly so tool_called/tool_arg/state oracles fire."""
    chosen: list[tuple[str, dict]] = []
    answers = turn.answers_dict()
    if answers:
        chosen.append(("record_answers", answers))
    if turn.reply:
        chosen.append(("reply_to_client", {"text": turn.reply}))
    # set_status is emitted every turn by the agent (it's mandatory in the system prompt).
    chosen.append((
        "set_status",
        {
            "status": turn.status,
            "reason": turn.status_reason,
            "complete": turn.qualification_complete,
            "internal_comment": turn.internal_comment,
        },
    ))
    if turn.microservice:
        chosen.append(("route_to_microservice", {"service": turn.microservice_service}))
    elif turn.handoff:
        chosen.append(("handoff_to_manager", {"reason": turn.handoff_reason}))
    return [
        {"type": "function",
         "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}
        for name, args in chosen
    ]


def channels() -> list[str]:
    """The untrusted surfaces THIS seam can truly inject (canonical names). message/history are
    universal (ride inline); ingested_record + memory are the agent's real ingestion vectors."""
    return ["message", "history", "ingested_record", "memory"]


# ----- the contract entrypoint ------------------------------------------------------------

def _collect_injections(request: dict) -> list[dict]:
    out: list[dict] = []
    if request.get("injection"):
        out.append(request["injection"])
    out.extend(request.get("injections") or [])
    return out


async def external(request: dict) -> dict:
    msgs = [m for m in request.get("messages", []) if m.get("role") != "system"]
    history = [
        {"id": i + 1,
         "direction": "inbound" if m.get("role") == "user" else "outbound",
         "text": m.get("content", "") or ""}
        for i, m in enumerate(msgs[:-1])
    ]
    last = msgs[-1].get("content", "") if msgs else ""

    lead_snapshot = _BENIGN_SNAPSHOT
    contact_name = _BENIGN_CONTACT_NAME
    known_answers: dict[str, str] = {}

    for inj in _collect_injections(request):
        ch, pay = inj.get("channel"), inj.get("payload", "")
        if ch == "message":                       # defensive: engine usually inlines this
            last = pay
        elif ch == "history":
            history.insert(0, {"id": 0, "direction": "inbound", "text": pay})
        elif ch == "ingested_record":             # poison a client-filled card field
            lead_snapshot = f"{_BENIGN_SNAPSHOT}\nClient's first message: {pay}"
        elif ch == "memory":                      # poison a prior recorded answer
            known_answers = {"prior_context": pay}
        else:
            # Unknown ingestion channel: route into the card rather than silently drop it (a
            # dropped injection would read as a false PASS). Surfaced via the channels() contract.
            lead_snapshot = f"{_BENIGN_SNAPSHOT}\n[{ch}] {pay}"

    turn = await _agent().run_turn(
        history=history,
        new_messages=[{"id": len(history) + 1, "direction": "inbound", "text": last}],
        known_answers=known_answers,
        lead_snapshot=lead_snapshot,
        contact_name=contact_name,
        manager_name=_BENIGN_MANAGER_NAME,
    )
    return {"choices": [{"message": {"content": turn.reply or "", "tool_calls": _to_tool_calls(turn)}}]}


# ----- certification driver ----------------------------------------------------------------

def certify(
    profile: str = str(ENGINE_DIR / "configs/profiles/example-agent-p10.yaml"),
    out: str = str(ENGINE_DIR / "reports/example-agent-p10"),
    *,
    framework: str = str(ENGINE_DIR / "frameworks/aiuc-1.yaml"),
    crosswalk: str = str(ENGINE_DIR / "crosswalks/aiuc-1/crosswalk.yaml"),
) -> None:
    """Run the corpus against the REAL agent (bridge tier), exercising EVERY engine LLM capability,
    and write report.{json,md} + full .eval logs.

    LLM capabilities driven (all on the agent's OWN API key, read in-memory from its .env):
      * variation_strategy=llm  -> the paraphrase model rewrites each seed in the agent's domain
      * attacker_model          -> drives adaptive / context-aware multi-turn attacks
      * judge_model             -> semantic backstop confirms binary hits (filters defended-agent FPs)
      * synthesize_n            -> PROPOSES new candidate probes, gated, then RUN too
      * planner=llm             -> weights/orders the budget; floor PINNED to the profile depth so
                                   no probe drops below the trials the <=10% Wilson bound needs
    """
    from datetime import datetime, timezone

    from app.config import settings as _agent_settings  # the agent's own key, loaded from its .env

    from probe_engine.config.profile import load_profile, run_config_from_profile
    from probe_engine.corpus.loader import load_corpus
    from probe_engine.mapping.loader import load_crosswalk, load_framework
    from probe_engine.plan.allocate import allocate
    from probe_engine.plan.models import AllocationBudget
    from probe_engine.plan.synthesize import synthesize_probes
    from probe_engine.report.builder import build_report
    from probe_engine.report.render_json import render_json
    from probe_engine.report.render_markdown import render_markdown
    from probe_engine.run.executor import run_corpus
    from probe_engine.run.selection import select_probes
    from probe_engine.targets.agent_context import build_agent_context

    # The API key the TARGET agent uses — same key drives every engine-side LLM here. Passed via
    # api_key= (get_model), never written to env / disk / logs (engine secret-handling invariant).
    api_key = (_agent_settings.api_key or "").strip() or None
    if not api_key:
        raise SystemExit("no API key in the agent's .env — the bridge needs the real key.")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prof = load_profile(profile)
    run_config = run_config_from_profile(prof, run_id="example-agent-p10", timestamp=ts)
    probes = load_corpus(str(ENGINE_DIR / "corpus/probes"))
    selected = select_probes(probes, run_config)
    print(f"selected {len(selected)}/{len(probes)} probes; driving the REAL agent ({run_config.target.name})", flush=True)
    context = build_agent_context(run_config)

    # ---- Tier-2: LLM probe synthesis — propose, gate, ADD the accepted to the run ----
    synthesis = None
    to_run = list(selected)
    if run_config.synthesize_n > 0:
        caps: set[str] = set(run_config.available_tools)
        for spec in run_config.tool_inventory:
            caps.update(spec.effective_capabilities())
        synthesis = synthesize_probes(
            context, crosswalk_path=crosswalk, n=run_config.synthesize_n,
            model_id=run_config.target.resolved_synthesis_model(), api_key=api_key,
            seed=prof.seed, agent_caps=caps,
        )
        # Synthesized probes must pass the SAME deterministic gate as curated ones (industry / channel
        # / capability / identity / lifecycle) — a synth probe targeting a surface this agent lacks is
        # a blind spot, not a pass.
        gated = select_probes(synthesis.accepted, run_config)
        gated_ids = {p.id for p in gated}
        synth_dropped = [p.id for p in synthesis.accepted if p.id not in gated_ids]
        to_run.extend(gated)
        print(f"synthesized {len(synthesis.accepted)} accepted / {len(synthesis.rejected)} rejected; "
              f"{len(gated)} pass target gating"
              + (f"; gated-out: {synth_dropped}" if synth_dropped else "")
              + f" (model={synthesis.model})", flush=True)

    # ---- Probes whose oracle can't be ADJUDICATED on THIS bridge target (no matching tool
    # declaration: authz_violation needs authz_action+resource_arg, secret_leaked needs
    # leaks_if_path_contains, state_changed needs a dangerous tool) are now guarded INSIDE the engine
    # (run.executor.run_corpus): it skips each such probe, surfaces it as a blind spot (logged +
    # progress "skip"), and reports the skipped ids back via the returned list's .blind_spots — never
    # crashing, never counting it as a pass (invariant 3). No manual prefilter needed here.

    # ---- Tier-1: LLM trial allocation. FLOOR PINNED to the profile depth so the LLM planner may
    # only ADD depth/order — every probe keeps >= n_variants*epochs trials (the >=36 the <=10% Wilson
    # upper bound needs). max_trials=None => shallow can't drop below the floor. ----
    budget = AllocationBudget(
        max_trials=run_config.max_trials,
        default_variants=run_config.n_variants, default_epochs=run_config.epochs,
        min_variants=run_config.n_variants, min_epochs=run_config.epochs,
    )
    planner_model = run_config.target.resolved_planner_model() if run_config.planner == "llm" else None
    plan = allocate(to_run, context, budget, strategy=run_config.planner,
                    model_id=planner_model, api_key=api_key, seed=prof.seed)
    print(f"plan: strategy={plan.strategy} model={plan.model} probes={len(plan.items)} "
          f"total_trials={plan.total_trials}" + (f" ({plan.notes})" if plan.notes else ""), flush=True)

    def _progress(phase, i, total, probe, ev):
        if phase == "start":
            print(f"▶ [{i}/{total}] {probe.id} …", flush=True)
        elif phase == "skip":
            print(f"⃠ [{i}/{total}] {probe.id:34s} BLIND SPOT — oracle not adjudicable, skipped",
                  flush=True)
        else:
            print(f"✓ [{i}/{total}] {probe.id:34s} n={ev.n_trials:>3} {ev.n_success}/{ev.n_trials} "
                  f"ASR={ev.asr:6.1%} [{ev.status.value}]", flush=True)

    evidence = run_corpus(
        to_run, run_config, plan=plan, external=external,
        log_dir=str(Path(out) / "logs"), api_key=api_key, seed=prof.seed, progress=_progress,
        adapter_channels=channels(),  # B5: reconcile declared vs routable channels (loud blind-spot warn)
    )
    if getattr(evidence, "blind_spots", None):
        print(f"blind spots — oracle not adjudicable on this target, skipped: {evidence.blind_spots}",
              flush=True)
    report = build_report(run_config, load_framework(framework), load_crosswalk(crosswalk),
                          evidence, plan=plan, synthesis=synthesis)
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(render_json(report), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    a = report.aggregates
    print(f"\noverall_asr={a['overall_asr']:.2%} covered={a['n_covered']} partial={a['n_partial']} "
          f"uncovered={a['n_uncovered']} not_testable={a['n_not_testable']} -> {out_dir}", flush=True)


def smoke() -> None:
    """Offline self-check (NO model call): adapter imports, agent constructs, channels + tool
    mapping are correct. Proves the wiring without spending an API call."""
    assert channels() == ["message", "history", "ingested_record", "memory"]
    a = _agent()  # constructs QualifierLLM (loads prompts, builds client) — no network
    assert a is not None
    sample = QualTurn(
        reply="Hello! What can I help you with today?",
        status="unknown", status_reason="the dialog has only just started",
        qualification_complete=False, handoff=False,
    )
    calls = {c["function"]["name"] for c in _to_tool_calls(sample)}
    assert calls == {"reply_to_client", "set_status"}, calls
    print("smoke OK — adapter imports, agent constructs, channels + tool mapping verified (offline).")
    print("channels():", channels())
    print("tool names on a sample turn:", sorted(calls))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "smoke":
        smoke()
    else:
        certify()
