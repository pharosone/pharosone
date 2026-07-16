"""FastAPI web UI: configure and run scans, stream live per-probe progress (SSE),
view the audit-ready report. The per-run API key is used only in-process for the
target model and is NEVER stored, logged, or returned in any response."""

import asyncio
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.enums import ScenarioType
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.mapping.loader import load_crosswalk, load_framework
from probe_engine.report.builder import build_report
from probe_engine.report.model import Report
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown
from probe_engine.run.executor import EvidenceList, ProbeExecutionError, run_probe
from probe_engine.run.selection import scope_excluded, select_probes
from probe_engine.targets.mock import MockPolicy

_STATIC = Path(__file__).parent / "static"

DEFAULT_CORPUS = "corpus/probes"
DEFAULT_FRAMEWORK = "frameworks/aiuc-1.yaml"
DEFAULT_CROSSWALK = "crosswalks/aiuc-1/crosswalk.yaml"


class RunRequest(BaseModel):
    tier: str = "mock"
    endpoint: str | None = None  # bridge tier: real agent's OpenAI-compatible HTTP URL
    provider: str | None = None
    model: str | None = None
    attacker_model: str | None = None  # adaptive probes: LLM that drives the attack
    paraphrase_model: str | None = None  # llm variation: model that rephrases prompts
    judge_model: str | None = None  # semantic backstop; set for a defended agent (else FP-prone
    # oracles degrade to UNVERIFIED rather than overstating leaks)
    fail_fast: bool = False  # early-exit: stop a probe's trials once a FAIL is statistically certain
    api_key: str | None = None
    # Keys-in-env path (used by the guided skill flow): instead of placing the secret in this
    # request body, name the ENV VAR holding it. The server reads the value from its own
    # environment in-process, so no key ever crosses the wire. Falls back to OPENROUTER_API_KEY.
    api_key_env: str | None = None
    system_prompt: str | None = None
    agent_description: str | None = None  # free-text agent profile -> tailors llm/adaptive attacks
    industry: str = "any"
    tools: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en"])
    variation_strategy: str = "deterministic"
    n_variants: int = 5
    epochs: int = 2
    # Attack approaches (scenario families) to run: single_turn / chain / adaptive. None = all three.
    # Narrowing is a deliberate scope reduction — excluded approaches are reported "not tested (scope)".
    approaches: list[str] | None = None
    seed: int = 1
    mock_rule: str = "by_fingerprint"
    mock_threshold: int = 30
    corpus: str = DEFAULT_CORPUS
    framework: str = DEFAULT_FRAMEWORK
    crosswalk: str = DEFAULT_CROSSWALK


def _evidence_event(ev) -> dict:
    return {
        "probe_id": ev.probe_id,
        "severity": ev.severity.value,
        "status": ev.status.value,
        "asr": ev.asr,
        "ci_low": ev.ci_low,
        "ci_high": ev.ci_high,
        "n_trials": ev.n_trials,
        "n_success": ev.n_success,
        "n_errors": ev.n_errors,
        "power": ev.power,
        "tags": [f"{t.system.value}:{t.id}" for t in ev.taxonomy_tags],
        "scenario": ev.scenario,
        "n_turns": ev.n_turns,
        "transcript": ev.transcript,
    }


def _resolve_key(req: RunRequest) -> str | None:
    """Resolve the per-run API key WITHOUT requiring it to cross the wire in a request body:
    prefer an explicit ``api_key`` (legacy/local form), else read the named env var
    (``api_key_env``), else fall back to ``OPENROUTER_API_KEY``. The value is used in-process
    only and is NEVER stored, logged, or emitted."""
    if req.api_key:
        return req.api_key
    if req.api_key_env:
        v = os.environ.get(req.api_key_env)
        if v:
            return v
    return os.environ.get("OPENROUTER_API_KEY") or None


def run_scan(
    req: RunRequest, emit: Callable[[str, dict], None], log_dir: str | None = None
) -> dict:
    """Load, select, run each probe (emitting progress per probe), build the report.

    Returns the report as a dict. The api_key is resolved (request or env), passed straight
    to run_probe, and never placed into any emitted event or the returned dict.
    """
    probes = load_corpus(req.corpus)
    fw = load_framework(req.framework)
    cw = load_crosswalk(req.crosswalk)
    run_config = RunConfig(
        target=TargetConfig(
            tier=req.tier, name=f"{req.tier}-target", endpoint=req.endpoint,
            provider=req.provider, model=req.model, attacker_model=req.attacker_model,
            paraphrase_model=req.paraphrase_model, judge_model=req.judge_model,
            system_prompt=req.system_prompt, description=req.agent_description,
        ),
        industry=req.industry,
        available_tools=req.tools,
        languages=req.languages or ["en"],
        variation_strategy=req.variation_strategy,
        n_variants=req.n_variants,
        epochs=req.epochs,
        approaches=req.approaches or [s.value for s in ScenarioType],
        fail_fast=req.fail_fast,
        corpus_version="seed",
        thresholds=Thresholds(),
        run_id=uuid.uuid4().hex[:12],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    selected = select_probes(probes, run_config)
    excluded = scope_excluded(probes, run_config)
    emit(
        "started",
        {
            "run_id": run_config.run_id,
            "tier": req.tier,
            "industry": req.industry,
            "selected": len(selected),
            "total": len(probes),
            # Deliberate scope reductions: approaches (scenario families) not run, disclosed up front
            # so the live view and report never read a narrowed run as robust against them.
            "excluded_approaches": sorted({p.scenario.type.value for p in excluded}),
            "scope_excluded": len(excluded),
            "probes": [
                {
                    "probe_id": p.id,
                    "title": p.title,
                    "severity": p.severity.value,
                    "scenario": p.scenario.type.value,
                    "n_turns": (
                        p.scenario.max_turns
                        if p.scenario.type.value == "adaptive"
                        else (len(p.scenario.turns) or 1)
                    ),
                }
                for p in selected
            ],
        },
    )
    if not selected:
        emit("error", {"message": "no probes apply to this run configuration"})
        return {}

    policy = MockPolicy(rule=req.mock_rule, threshold=req.mock_threshold)
    # EvidenceList so build_report can surface any errored probes (mirrors run_corpus' honesty).
    evidences = EvidenceList()
    errored: list[str] = []
    for i, probe in enumerate(selected):
        emit("probe_start", {"probe_id": probe.id, "index": i, "total": len(selected)})
        # A probe whose every sample errored on the target must not abort the whole scan — surface it
        # and continue (never counted as a pass/robust; disclosed in the report's errored_probes).
        try:
            ev = run_probe(
                probe,
                run_config,
                mock_policy=policy,
                seed=req.seed,
                log_dir=log_dir,
                api_key=_resolve_key(req),
            )
        except ProbeExecutionError as exc:
            errored.append(probe.id)
            emit("probe_error", {"index": i, "total": len(selected), "probe_id": probe.id,
                                 "message": str(exc)})
            continue
        evidences.append(ev)
        emit("probe_done", {"index": i, "total": len(selected), **_evidence_event(ev)})

    evidences.errored = errored
    report = build_report(run_config, fw, cw, evidences, scope_excluded=excluded)
    report_dict = json.loads(render_json(report))
    emit("done", {"aggregates": report_dict["aggregates"], "scope": report_dict["scope"]})
    return report_dict


# In-memory run registry. Each run: events list, done flag, report dict, markdown.
RUNS: dict[str, dict] = {}


def _start_run(req: RunRequest) -> str:
    run_id = uuid.uuid4().hex[:12]
    state: dict = {"events": [], "done": False, "report": None, "markdown": None}
    RUNS[run_id] = state

    def emit(kind: str, data: dict) -> None:
        state["events"].append({"event": kind, "data": data})

    def worker() -> None:
        try:
            report_dict = run_scan(req, emit, log_dir=f"logs/web/{run_id}")
            if report_dict:
                state["report"] = report_dict
                state["markdown"] = render_markdown(Report.model_validate(report_dict))
        except Exception as exc:  # surface any failure to the UI
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            state["done"] = True

    threading.Thread(target=worker, daemon=True).start()
    return run_id


async def _event_stream(run_id: str):
    state = RUNS.get(run_id)
    if state is None:
        return
    idx = 0
    while True:
        events = state["events"]
        while idx < len(events):
            ev = events[idx]
            idx += 1
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'])}\n\n"
        if state["done"] and idx >= len(state["events"]):
            yield "event: end\ndata: {}\n\n"
            break
        await asyncio.sleep(0.15)


# Self-contained live-progress page for a run STARTED elsewhere (e.g. the guided skill flow
# POSTs /run, then hands the user /watch/<run_id>). No dependency on the static form UI: it just
# attaches to the run's SSE stream and renders per-probe progress + the report links on completion.
_WATCH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>PharosOne — run __RUN_ID__</title>
<style>
 body{font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:24px;background:#0b0e14;color:#cdd6f4}
 h1{font-size:16px;color:#89b4fa} .muted{color:#6c7086} .ok{color:#a6e3a1} .fail{color:#f38ba8}
 .unver{color:#f9e2af} .row{padding:2px 0;border-bottom:1px solid #1e2030} #bar{margin:10px 0;color:#94e2d5}
 a{color:#89b4fa} #report{margin-top:16px}
</style></head><body>
<h1>PharosOne Probe Engine — live run</h1>
<div class="muted">run <b>__RUN_ID__</b></div>
<div id="bar">connecting…</div>
<div id="log"></div>
<div id="report"></div>
<script>
 const rid="__RUN_ID__", bar=document.getElementById("bar"),
       log=document.getElementById("log"), rep=document.getElementById("report");
 let total=0, done=0;
 const es=new EventSource("/events/"+rid);
 const pct=()=>total?Math.round(100*done/total):0;
 es.addEventListener("started",e=>{const d=JSON.parse(e.data);total=d.selected;
   bar.textContent=`selected ${d.selected}/${d.total} probes — running…`;});
 es.addEventListener("probe_start",e=>{const d=JSON.parse(e.data);
   bar.textContent=`[${d.index+1}/${d.total}] ${pct()}% — ${d.probe_id} …`;});
 es.addEventListener("probe_done",e=>{const d=JSON.parse(e.data);done++;
   const cls=d.status==="fail"?"fail":(d.status==="unverified"?"unver":"ok");
   const div=document.createElement("div");div.className="row";
   div.innerHTML=`<span class="${cls}">${d.status==="fail"?"✗":(d.status==="unverified"?"?":"✓")}</span> `+
     `${d.probe_id} — ${d.n_success}/${d.n_trials} ASR ${(100*d.asr).toFixed(1)}% `+
     `<span class="muted">CI[${(100*d.ci_low).toFixed(1)}–${(100*d.ci_high).toFixed(1)}%] ${d.status}</span>`;
   log.appendChild(div);bar.textContent=`${done}/${total} done (${pct()}%)`;});
 es.addEventListener("done",e=>{const d=JSON.parse(e.data);
   bar.innerHTML=`<span class="ok">complete</span> — ${done}/${total} probes`;
   rep.innerHTML=`<h1>Report</h1><a href="/report/${rid}/markdown" target="_blank">Markdown</a> · `+
     `<a href="/report/${rid}" target="_blank">JSON</a><pre>${JSON.stringify(d.aggregates,null,2)}</pre>`;});
 es.addEventListener("error",e=>{try{const d=JSON.parse(e.data);
   bar.innerHTML=`<span class="fail">error</span> — ${d.message||"see logs"}`;}catch(_){}});
 es.addEventListener("end",()=>es.close());
</script></body></html>"""


def create_app() -> FastAPI:
    app = FastAPI(title="PharosOne Probe Engine")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_STATIC / "index.html")

    @app.get("/watch/{run_id}")
    def watch(run_id: str) -> HTMLResponse:
        if run_id not in RUNS:
            raise HTTPException(status_code=404, detail="unknown run")
        return HTMLResponse(_WATCH_HTML.replace("__RUN_ID__", run_id))

    @app.get("/meta")
    def meta(corpus: str = DEFAULT_CORPUS) -> dict:
        probes = load_corpus(corpus)
        industries = sorted({i for p in probes for i in p.applicability.industries})
        tools = sorted({t for p in probes for t in p.applicability.required_tools})
        return {"probe_count": len(probes), "industries": industries, "tools": tools}

    @app.post("/run")
    def start(req: RunRequest) -> dict:
        return {"run_id": _start_run(req)}

    @app.get("/events/{run_id}")
    def events(run_id: str) -> StreamingResponse:
        if run_id not in RUNS:
            raise HTTPException(status_code=404, detail="unknown run")
        return StreamingResponse(_event_stream(run_id), media_type="text/event-stream")

    @app.get("/status/{run_id}")
    def status(run_id: str) -> dict:
        state = RUNS.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown run")
        return {
            "done": state["done"],
            "n_events": len(state["events"]),
            "has_report": state["report"] is not None,
        }

    @app.get("/report/{run_id}")
    def report(run_id: str):
        state = RUNS.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown run")
        if state["report"] is None:
            return JSONResponse({"done": state["done"]}, status_code=202)
        return state["report"]

    @app.get("/report/{run_id}/markdown")
    def report_markdown(run_id: str):
        state = RUNS.get(run_id)
        if state is None or not state.get("markdown"):
            raise HTTPException(status_code=404, detail="no report yet")
        return PlainTextResponse(state["markdown"], media_type="text/markdown")

    return app


app = create_app()
