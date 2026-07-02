"""Probe selection: industry / available tools / severity are EXECUTION parameters
that decide which probes apply, not just report filters (spec §4)."""

from probe_engine.domain.enums import Severity
from probe_engine.domain.probe import Probe
from probe_engine.domain.run import RunConfig
from probe_engine.targets.capabilities import provided_capabilities

_SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


def probe_applies(probe: Probe, run_config: RunConfig) -> bool:
    """Does this probe apply to the configured run context?"""
    app = probe.applicability

    # Industry: a non-"any" run keeps only universal ("any") probes and probes scoped
    # to that industry. An "any" run applies no industry filter.
    if run_config.industry != "any":
        industries = app.industries or ["any"]
        if "any" not in industries and run_config.industry not in industries:
            return False

    # Tools: cannot test abuse of a capability the agent does not have. A probe's required_tools
    # are canonical CAPABILITIES; they must be a subset of what the agent provides (resolved
    # through tool_inventory.capabilities, e.g. a concrete tool mapped to send_message). An empty
    # inventory AND no available_tools means "do not filter on tools".
    if run_config.available_tools or run_config.tool_inventory:
        if not set(app.required_tools).issubset(provided_capabilities(run_config)):
            return False

    # Delivery channels: a probe that delivers poison through an ingestion channel (e.g.
    # `ingested_record`, `tool_result`) only applies to a target that DECLARES that channel
    # (target.channels). `message`/`history` are universal (every target has the conversation), so
    # channel-less probes are unaffected. An undeclared channel = a blind spot -> skip (never
    # silently re-route the poison to the message channel and read it as covered).
    used_channels = {t.channel for t in probe.scenario.turns if t.poison}
    available = set(run_config.target.channels) | {"message", "history"}
    if not used_channels.issubset(available):
        return False

    # Identity context: an authz probe needs the target to declare WHO the agent acts as (an
    # acting principal), so cross-owner access can be adjudicated. Undeclared = blind spot -> skip
    # (never a silent pass). Probes that don't require it (default) are unaffected — so an agent
    # with no authz surface, like a lead qualifier, gates OUT of authz exactly like a capability it
    # lacks, while still running stage-only state probes.
    if probe.applicability.requires_identity_context and run_config.target.acting_principal is None:
        return False

    # Lifecycle context: a state-invariant probe needs the target to declare a lifecycle (seed_stage),
    # exactly as an authz probe needs a declared principal. Undeclared = blind spot -> skip. This
    # clause sits OUTSIDE the capability no-filter branch, so it holds even when tool filtering is
    # skipped (an empty-inventory mock smoke must not silently pull in lifecycle probes).
    if probe.applicability.requires_lifecycle_context and run_config.target.seed_stage is None:
        return False

    # Severity floor.
    if _SEVERITY_RANK[probe.severity] < _SEVERITY_RANK[run_config.severity_floor]:
        return False

    return True


def reconcile_channels(declared: set[str], routable: set[str] | None) -> dict:
    """Reconcile profile-declared delivery channels against the channels the adapter can
    actually route poison into. Returns:
      {"tested": <sorted list>,                 # channels we will actually test
       "declared_not_routable": <sorted list>,  # FALSE coverage: declared but adapter can't deliver
       "routable_not_declared": <sorted list>}  # missed coverage: adapter could but profile omits
    routable is None  -> no adapter info: tested = sorted(declared), both diff lists empty.
    "message" and "history" are universal (every conversational target has them): when routable
    is not None, treat them as always routable."""
    if routable is None:
        # No adapter info — trust the profile as-is; nothing to reconcile.
        return {
            "tested": sorted(declared),
            "declared_not_routable": [],
            "routable_not_declared": [],
        }

    # message/history are universal: every conversational target can route them.
    effective_routable = set(routable) | {"message", "history"}

    # We test only what is both declared AND actually routable.
    tested = declared & effective_routable
    # Declared but the adapter cannot deliver — false coverage if we believed the profile.
    declared_not_routable = declared - effective_routable
    # Adapter could deliver but the profile never declared it — missed coverage.
    routable_not_declared = effective_routable - declared
    return {
        "tested": sorted(tested),
        "declared_not_routable": sorted(declared_not_routable),
        "routable_not_declared": sorted(routable_not_declared),
    }


def select_probes(probes: list[Probe], run_config: RunConfig) -> list[Probe]:
    return [p for p in probes if probe_applies(p, run_config)]
