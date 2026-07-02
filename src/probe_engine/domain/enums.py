"""Closed vocabularies used across the domain."""

from enum import Enum


class TaxonomySystem(str, Enum):
    ATLAS = "atlas"
    OWASP_AGENTIC = "owasp_agentic"
    CWE = "cwe"


class EvidenceType(str, Enum):
    BEHAVIORAL = "behavioral"
    CONFIG = "config"
    DOCUMENT = "document"
    TELEMETRY = "telemetry"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ScenarioType(str, Enum):
    SINGLE_TURN = "single_turn"
    CHAIN = "chain"
    ADAPTIVE = "adaptive"


class EvidenceStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INSUFFICIENT_POWER = "insufficient_power"
    NOT_RUN = "not_run"
    # The probe ran, but its verdict cannot be trusted as a confident pass/fail because the decision
    # rested on a false-positive-prone BINARY oracle that no judge adjudicated (e.g. `prompt_leak` /
    # `contains` over-firing on a defended agent's refusals). n_success/asr stay populated for
    # transparency, but the status MUST NOT read as a confident fail nor a robust pass — a judge is
    # required. Mirrors the existing loud-degrade principle already used for a configured-but-
    # UNAVAILABLE judge (scoring/judge.py JudgeUnavailable + batch_judge_with_status).
    UNVERIFIED = "unverified"


class CoverageStatus(str, Enum):
    COVERED = "covered"            # density met, all contributing evidence passing-or-measured
    PARTIAL = "partial"            # some evidence but density below threshold
    UNCOVERED = "uncovered"        # behaviorally testable, zero probes reached it
    NOT_TESTABLE = "not_testable"  # control not behaviorally testable (needs other evidence source)


class VariationStrategy(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM = "llm"
