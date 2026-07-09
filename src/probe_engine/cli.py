"""Command-line interface for the Probe Engine (spec §7)."""

import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import typer
import yaml

from probe_engine.config.profile import load_profile, run_config_from_profile
from probe_engine.corpus.loader import load_corpus
from probe_engine.domain.enums import ScenarioType
from probe_engine.domain.run import RunConfig, TargetConfig, Thresholds
from probe_engine.mapping.loader import load_crosswalk, load_framework
from probe_engine.plan.allocate import allocate
from probe_engine.plan.models import AllocationBudget
from probe_engine.plan.synthesize import synthesize_probes
from probe_engine.report.builder import build_report
from probe_engine.report.render_json import render_json
from probe_engine.report.render_markdown import render_markdown
from probe_engine.run.executor import run_corpus
from probe_engine.run.selection import scope_excluded, select_probes
from probe_engine.scoring.unverified import FP_PRONE_ORACLES, probe_oracle_kind
from probe_engine.targets.agent_context import build_agent_context
from probe_engine.targets.mock import MockPolicy


class ArtifactKind(str, Enum):
    passport = "passport"
    seams = "seams"


app = typer.Typer(help="PharosOne Probe Engine")


def _warn_unverified_probes(selected, run_config) -> None:
    """GUARD: warn LOUDLY when the SELECTED set contains false-positive-prone binary probes
    (`prompt_leak` / `contains`) but the run has no `judge_model` — those probes WILL be reported as
    UNVERIFIED (not pass/fail), because binary-only they over-fire on a defended agent's refusals.
    For `prompt_leak`, also call out an empty `protected_snippets` (no real reference). Print-only,
    no behavior change — purely a heads-up so a customer configures a judge before certifying."""
    if run_config.target.resolved_judge_model():
        return  # a judge is configured -> these probes get a real verdict, nothing to warn about.
    affected = sorted(
        p.id for p in selected if probe_oracle_kind(p) in FP_PRONE_ORACLES
    )
    if not affected:
        return
    typer.echo(
        f"WARNING: {len(affected)} selected probe(s) use a false-positive-prone binary oracle "
        f"({'/'.join(sorted(FP_PRONE_ORACLES))}) but no judge_model is configured — these will be "
        f"reported UNVERIFIED (judge required), NOT pass/fail: {', '.join(affected)}"
    )
    if not run_config.target.protected_snippets:
        pl = sorted(p.id for p in selected if probe_oracle_kind(p) == "prompt_leak")
        if pl:
            typer.echo(
                "WARNING: 'prompt_leak' probe(s) have EMPTY protected_snippets and no judge_model — "
                f"there is no real protected reference; these stay UNVERIFIED: {', '.join(pl)}"
            )


@app.command()
def validate(
    corpus: str = typer.Option(..., help="probes directory"),
    framework: str = typer.Option(..., help="framework YAML"),
    crosswalk: str = typer.Option(..., help="crosswalk YAML"),
) -> None:
    """Load and consistency-check corpus, framework, and crosswalk."""
    probes = load_corpus(corpus)
    fw = load_framework(framework)
    cw = load_crosswalk(crosswalk)
    referenced = {r.control_id for e in cw.entries for r in e.controls}
    missing = sorted(referenced - fw.control_ids())
    typer.echo(f"probes={len(probes)} controls={len(fw.controls)} entries={len(cw.entries)}")
    if missing:
        typer.echo(f"ERROR: crosswalk references unknown controls: {missing}")
        raise typer.Exit(code=1)
    typer.echo("OK")


@app.command()
def run(
    corpus: str = typer.Option(...),
    framework: str = typer.Option(...),
    crosswalk: str = typer.Option(...),
    out: str = typer.Option("reports/out"),
    profile: str = typer.Option(None, help="run profile YAML; supersedes the flags below"),
    tier: str = typer.Option("mock", help="mock | model | bridge"),
    endpoint: str = typer.Option(
        None, help="bridge tier: OpenAI-compatible HTTP URL of the REAL agent under test"
    ),
    provider: str = typer.Option(
        None, help="gateway provider, e.g. openrouter (model becomes openrouter/<slug>)"
    ),
    model: str = typer.Option(
        None, help="model id, e.g. anthropic/claude-opus-4-8 or (openrouter) anthropic/claude-3.5-sonnet"
    ),
    api_key: str = typer.Option(
        None, help="API key — model tier (else env) or bridge endpoint Bearer token"
    ),
    attacker_model: str = typer.Option(None, help="adaptive: attacker LLM (defaults to model)"),
    variation_strategy: str = typer.Option(
        "deterministic", help="prompt variation: deterministic | llm (LLM paraphrase)"
    ),
    paraphrase_model: str = typer.Option(
        None, help="llm variation: model that rephrases prompts (defaults to model on model tier)"
    ),
    system_prompt: str = typer.Option(None, help="system prompt of the agent under test"),
    system_prompt_file: str = typer.Option(None, help="file holding the agent's system prompt"),
    agent_description: str = typer.Option(
        None, help="free-text profile of the agent — tailors llm/adaptive attacks to it"
    ),
    agent_description_file: str = typer.Option(None, help="file holding the agent description"),
    industry: str = typer.Option("any", help="vertical: financial_services, healthcare, ..."),
    tools: str = typer.Option("", help="comma-separated tool inventory the agent actually has"),
    languages: str = typer.Option("en", help="comma-separated language codes (default: en)"),
    planner: str = typer.Option(
        "deterministic",
        help="trial allocation across eligible probes: deterministic (uniform) | llm (Opus weights)",
    ),
    max_trials: int = typer.Option(
        None, help="optional global attack budget the planner scales allocations to fit"
    ),
    planner_model: str = typer.Option(
        None, help="llm planner: model that weights probes (defaults to Opus 4.8)"
    ),
    synthesize: int = typer.Option(
        0, help="number of NEW candidate probes the synthesis LLM proposes (0 = off)"
    ),
    synthesis_model: str = typer.Option(
        None, help="model that proposes synthesized probes (defaults to Opus 4.8)"
    ),
    save_generated: str = typer.Option(
        None, help="directory to persist each accepted synthesized probe as YAML (audit/pin)"
    ),
    n_variants: int = typer.Option(5),
    epochs: int = typer.Option(2),
    approaches: str = typer.Option(
        None,
        help="comma-separated attack approaches to run: single_turn,chain,adaptive (default: all). "
        "Narrowing is a deliberate scope reduction — excluded approaches are reported 'not tested "
        "(scope)', never as robust.",
    ),
    seed: int = typer.Option(1),
    mock_rule: str = typer.Option("by_fingerprint"),
    mock_threshold: int = typer.Option(30),
    log_dir: str = typer.Option("logs"),
    display: str = typer.Option(
        "none",
        help="live output: none (silent) | rich/full (live dashboard) | conversation (live transcripts) | plain | log",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="B3: reuse per-probe checkpoints under <out>/.checkpoint (config-hash matched) and skip "
        "probes already run — resumes an interrupted run without redoing expensive probes",
    ),
    fail_fast: bool = typer.Option(
        False,
        "--fail-fast",
        help="stop a probe's trials early once a FAIL is statistically certain (Wilson lower bound "
        "of ASR >= asr_pass) — saves the battery on an agent that breaks immediately; never changes "
        "a pass verdict. ASR is then measured on the partial sample (ci_low is the proven floor).",
    ),
) -> None:
    """Run the corpus against the target and write an audit-ready report."""
    probes = load_corpus(corpus)
    fw = load_framework(framework)
    cw = load_crosswalk(crosswalk)
    run_id = uuid.uuid4().hex[:12]
    ts = datetime.now(timezone.utc).isoformat()

    if profile:
        prof = load_profile(profile)
        run_config = run_config_from_profile(prof, run_id, ts)
        policy = MockPolicy(rule=prof.mock_rule, threshold=prof.mock_threshold)
        run_seed = prof.seed
    else:
        prompt_text = system_prompt
        if system_prompt_file:
            prompt_text = Path(system_prompt_file).read_text(encoding="utf-8")
        desc_text = agent_description
        if agent_description_file:
            desc_text = Path(agent_description_file).read_text(encoding="utf-8")
        run_config = RunConfig(
            target=TargetConfig(
                tier=tier, name=f"{tier}-target", endpoint=endpoint, provider=provider,
                model=model, attacker_model=attacker_model, paraphrase_model=paraphrase_model,
                planner_model=planner_model, synthesis_model=synthesis_model,
                system_prompt=prompt_text, description=desc_text,
            ),
            industry=industry,
            available_tools=[t.strip() for t in tools.split(",") if t.strip()],
            languages=[l.strip() for l in languages.split(",") if l.strip()] or ["en"],
            variation_strategy=variation_strategy,
            planner=planner,
            max_trials=max_trials,
            synthesize_n=synthesize,
            n_variants=n_variants,
            epochs=epochs,
            corpus_version="seed",
            thresholds=Thresholds(),
            run_id=run_id,
            timestamp=ts,
        )
        policy = MockPolicy(rule=mock_rule, threshold=mock_threshold)
        run_seed = seed

    if fail_fast:  # --fail-fast force-enables it on either path (a profile may also set it)
        run_config = run_config.model_copy(update={"fail_fast": True})

    if approaches is not None:  # --approaches narrows the run to the named scenario families
        chosen = [a.strip() for a in approaches.split(",") if a.strip()]
        valid = {s.value for s in ScenarioType}
        bad = [a for a in chosen if a not in valid]
        if bad:
            typer.echo(f"ERROR: unknown --approaches {bad}; valid: {sorted(valid)}")
            raise typer.Exit(code=1)
        if not chosen:
            typer.echo("ERROR: --approaches must name at least one of: " + ", ".join(sorted(valid)))
            raise typer.Exit(code=1)
        run_config = run_config.model_copy(update={"approaches": chosen})

    if run_config.target.tier == "bridge" and not run_config.target.endpoint:
        typer.echo(
            "ERROR: bridge tier needs --endpoint <url> (the real agent's OpenAI-compatible "
            "HTTP endpoint). For a framework agent, drive run_corpus(..., external=...) from Python."
        )
        raise typer.Exit(code=1)

    selected = select_probes(probes, run_config)
    typer.echo(
        f"selected {len(selected)}/{len(probes)} probes "
        f"(industry={run_config.industry}, tools={run_config.available_tools or 'all'})"
    )
    excluded = scope_excluded(probes, run_config)
    if excluded:
        by_type = sorted({p.scenario.type.value for p in excluded})
        detail = ", ".join(
            f"{t} ({sum(1 for p in excluded if p.scenario.type.value == t)})" for t in by_type
        )
        typer.echo(
            f"approaches excluded {len(excluded)} probe(s) by scope choice: {detail} "
            "— reported 'not tested (scope)', never robust"
        )
    if not selected:
        typer.echo("ERROR: no probes apply to this run configuration")
        raise typer.Exit(code=1)
    _warn_unverified_probes(selected, run_config)

    # The attack-side LLMs (planner + synthesizer) are told about the agent under test via this
    # context — same source of truth the variation/adaptive paths use. Offline-safe (no model call).
    context = build_agent_context(run_config)

    # ---- TIER-2: PROBE SYNTHESIS (optional; --synthesize N) ---------------------------------------
    # Propose NEW candidate probes tailored to this agent, gate each deterministically, and ADD the
    # accepted ones to the set we RUN (extra coverage — the universal corpus on disk is NOT touched).
    # Offline / no key: the synthesis model is resolved but generate() fails -> 0 accepted + a note,
    # so the command still completes (the on-disk corpus is unchanged; synthesize 0 => inert block).
    synthesis = None
    probes_to_run = list(selected)
    if run_config.synthesize_n > 0:
        # The agent's OWN declared capabilities widen the allowed required_tools vocabulary (UNION
        # the canonical capability set) so a synthesized probe may target this agent's real tools.
        agent_caps: set[str] = set(run_config.available_tools)
        for spec in run_config.tool_inventory:
            agent_caps.update(spec.effective_capabilities())
        synthesis = synthesize_probes(
            context,
            crosswalk_path=crosswalk,
            n=run_config.synthesize_n,
            model_id=run_config.target.resolved_synthesis_model(),
            api_key=api_key,
            seed=run_seed,
            agent_caps=agent_caps,
        )
        probes_to_run.extend(synthesis.accepted)
        typer.echo(
            f"synthesized {len(synthesis.accepted)} accepted / "
            f"{len(synthesis.rejected)} rejected "
            f"(model={synthesis.model}; {synthesis.notes})"
        )
        if save_generated and synthesis.accepted:
            gen_dir = Path(save_generated)
            gen_dir.mkdir(parents=True, exist_ok=True)
            for probe in synthesis.accepted:
                payload = probe.model_dump(mode="json", exclude_none=True)
                (gen_dir / f"{probe.id}.yaml").write_text(
                    yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
                    encoding="utf-8",
                )
            typer.echo(f"saved {len(synthesis.accepted)} generated probe(s) -> {gen_dir}")

    # ---- TIER-1: TRIAL ALLOCATION (planner) -------------------------------------------------------
    # Re-weight/order the attack budget WITHIN the probes we run; deterministic gating stays the
    # floor (every probe is allocated >= the floor, none dropped). max_trials=None + deterministic
    # => uniform == today. llm + no/failed model => deterministic fallback, so it runs offline.
    budget = AllocationBudget(
        max_trials=run_config.max_trials,
        default_variants=run_config.n_variants,
        default_epochs=run_config.epochs,
    )
    planner_model_id = (
        run_config.target.resolved_planner_model()
        if run_config.planner == "llm"
        else None
    )
    try:
        plan = allocate(
            probes_to_run,
            context,
            budget,
            strategy=run_config.planner,
            model_id=planner_model_id,
            api_key=api_key,
            seed=run_seed,
        )
    except Exception as exc:
        # OFFLINE FALLBACK (invariant): the LLM planner resolving a model that can't be reached
        # offline must NOT abort the run — fall back to the deterministic (uniform) allocation, the
        # same floor a deterministic run uses. The oracle still decides success either way.
        plan = allocate(probes_to_run, context, budget, strategy="deterministic", seed=run_seed)
        typer.echo(f"planner unavailable ({exc!r}); deterministic fallback")
    typer.echo(
        f"plan: strategy={plan.strategy} model={plan.model} "
        f"probes={len(plan.items)} total_trials={plan.total_trials}"
        + (f" ({plan.notes})" if plan.notes else "")
    )

    def _progress(phase, i, total, probe, ev):
        if phase == "start":
            typer.echo(f"▶ [{i}/{total}] {probe.id} …")
        elif phase == "skip":
            # B2 blind-spot skip: the target can't adjudicate this probe's oracle/channel. ev is
            # None — surface it as a skip (never a silent pass), and DON'T touch ev (would crash).
            typer.echo(f"⊘ [{i}/{total}] {probe.id}: SKIPPED (blind spot — not adjudicable on target)")
        else:
            typer.echo(
                f"✓ [{i}/{total}] {probe.id}: {ev.n_success}/{ev.n_trials} "
                f"ASR={ev.asr:.1%} [{ev.status.value}]"
                + (" early-stop" if ev.early_stopped else "")
            )

    evidence = run_corpus(
        probes_to_run, run_config, plan=plan, mock_policy=policy, seed=run_seed,
        log_dir=log_dir, api_key=api_key, display=display, progress=_progress,
        resume=resume, out_dir=str(out),
    )
    report = build_report(
        run_config, fw, cw, evidence, plan=plan, synthesis=synthesis, scope_excluded=excluded
    )

    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(render_json(report), encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")

    a = report.aggregates
    blind = f" blind_spots={len(report.blind_spots)}" if report.blind_spots else ""
    typer.echo(
        f"overall_asr={a['overall_asr']:.2%} covered={a['n_covered']} partial={a['n_partial']} "
        f"uncovered={a['n_uncovered']} not_testable={a['n_not_testable']}{blind} -> {out_dir}"
    )


@app.command()
def report(
    report_json: str = typer.Option(..., help="a report.json previously written by run"),
    out: str = typer.Option("reports/out/report.md"),
) -> None:
    """Re-render a saved report.json to Markdown."""
    from probe_engine.report.model import Report

    rep = Report.model_validate_json(Path(report_json).read_text(encoding="utf-8"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(render_markdown(rep), encoding="utf-8")
    typer.echo(f"rendered -> {out}")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="bind host"),
    port: int = typer.Option(8000, help="bind port"),
) -> None:
    """Launch the web UI (configure, run scans, watch live progress, view the report)."""
    import uvicorn

    from probe_engine.web.app import app as web_app

    typer.echo(f"Probe Engine UI -> http://{host}:{port}")
    uvicorn.run(web_app, host=host, port=port)


@app.command("validate-artifacts")
def validate_artifacts(
    kind: ArtifactKind = typer.Argument(..., help="artifact kind: passport | seams"),
    path: str = typer.Argument(..., help="path to a .json artifact or a .md file with a ```json block"),
) -> None:
    """Validate a pharosone onboarding artifact (passport/seams) against its schema + invariants."""
    from probe_engine.onboarding.validate import ArtifactError, load_artifact, validate

    p = Path(path)
    if not p.exists():
        typer.echo(f"error: file not found: {p}", err=True)
        raise typer.Exit(1)
    try:
        instance = load_artifact(p)
    except ArtifactError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1)
    errors = validate(kind.value, instance)
    if errors:
        typer.echo(f"INVALID: {p} ({kind.value}) — {len(errors)} problem(s):", err=True)
        for error in errors:
            typer.echo(f"  - {error}", err=True)
        raise typer.Exit(1)
    typer.echo(f"OK: {p} ({kind.value}) is valid.")


if __name__ == "__main__":
    app()
