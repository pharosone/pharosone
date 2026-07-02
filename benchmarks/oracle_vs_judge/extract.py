"""Read `*.eval` logs into minimal per-trial rows for the oracle-vs-judge analysis.

WHAT EACH TRIAL CARRIES (verified against `scoring/oracle.py` + `run/executor.py`):

  * `scoring.oracle.probe_oracle_scorer` stamps every sample's score metadata with `binary_hit`
    (the static oracle's provisional hit), `success` (provisional == binary_hit at scrape time),
    `transcript` / `tool_calls` / `reply`, and sets the score `explanation` to
    ``"oracle=<kind> binary=<bool> (judge-only success decided in batch pass)"``.
  * `run.executor._apply_batch_judge` then OVERWRITES `metadata["success"]` with the judge verdict
    and sets `metadata["judge_applied"]` True/False per trial (False on offline/no-judge/unavailable
    fallback, where `success == binary_hit`), plus `judge_status` / `judge_confirmed` /
    `judge_unverified` when a judge ran.

So the oracle-kind is recovered from the score EXPLANATION (`oracle=<kind>`), independent of the
corpus; `binary_hit` and `success` come straight from the score metadata; `judge_applied` is the
gate that says whether `success` is a real judge verdict.

SCHEMA ROBUSTNESS: older logs wrote a LEAN score schema (metadata = `{"success": ...}`, explanation
``"oracle=<kind> success=<bool>"``, no `binary_hit`, no `judge_applied`). For those we fall back to
the score VALUE as the binary signal and mark `judge_applied=False` — they are reported separately
(not informative for override analysis) but never crash the scan.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The oracle kind is the first token after `oracle=` in the score explanation, written by
# `probe_oracle_scorer` in BOTH the current ("oracle=X binary=Y ...") and legacy
# ("oracle=X success=Y") formats.
ORACLE_RE = re.compile(r"oracle=(\w+)")

# Storage truncations for the Part-B transcript/tools/reply (>= the batch judge's own block
# truncation of 2500/1500/1200 so the judge's truncation stays the binding one — the labeler sees
# the same input a live judge would).
_STORE_TRANSCRIPT = 3000
_STORE_TOOLS = 1800
_STORE_REPLY = 1500


@dataclass(frozen=True)
class TrialRow:
    """One scored trial projected to the fields the oracle-vs-judge analysis needs.

    `binary_hit` is the static oracle signal, `judge_success` the LLM judge verdict, and
    `judge_applied` says whether `judge_success` is a real judge verdict (vs an offline fallback
    where it just mirrors `binary_hit`). `binary_from_value` is True when `binary_hit` had to be
    recovered from the score value (legacy lean schema) rather than read from `metadata["binary_hit"]`."""

    log_file: str
    probe_id: str
    sample_id: str
    epoch: int
    oracle_kind: str
    binary_hit: bool
    judge_success: bool
    judge_applied: bool
    judge_unverified: bool
    binary_from_value: bool
    # Part-B payload (only needed for the <=200 sampled trials; small + truncated).
    transcript: str
    tool_calls: str
    reply: str

    @property
    def disagreement(self) -> bool:
        return self.binary_hit != self.judge_success

    def key(self) -> tuple[str, str, int]:
        """Stable identity for deterministic sampling / dedupe."""
        return (self.log_file, self.sample_id, self.epoch)

    def light_dict(self) -> dict:
        """Compact record WITHOUT the heavy transcript/tools/reply (for Part-A row dumps)."""
        return {
            "log_file": self.log_file,
            "probe_id": self.probe_id,
            "sample_id": self.sample_id,
            "epoch": self.epoch,
            "oracle_kind": self.oracle_kind,
            "binary_hit": self.binary_hit,
            "judge_success": self.judge_success,
            "judge_applied": self.judge_applied,
            "judge_unverified": self.judge_unverified,
            "binary_from_value": self.binary_from_value,
        }


def _probe_id_of(sample) -> str:
    meta = sample.metadata or {}
    pid = meta.get("probe_id")
    if pid:
        return str(pid)
    # Fall back to the variant id convention "<probe_id>#<n>".
    sid = str(getattr(sample, "id", "") or "")
    return sid.split("#", 1)[0] if "#" in sid else (sid or "?")


def _oracle_kind_of(explanation: str, probe_id: str, oracle_by_probe: dict[str, str] | None) -> str:
    m = ORACLE_RE.search(explanation or "")
    if m:
        return m.group(1)
    if oracle_by_probe and probe_id in oracle_by_probe:
        return oracle_by_probe[probe_id]
    return "?"


def rows_from_eval_log(
    log, log_name: str, *, oracle_by_probe: dict[str, str] | None = None
) -> list[TrialRow]:
    """Project every scored sample of one EvalLog into a :class:`TrialRow`.

    `oracle_by_probe` (probe_id -> oracle kind, from the corpus) is only used as a fallback when the
    score explanation does not carry `oracle=<kind>` (it always does in current + legacy logs)."""
    rows: list[TrialRow] = []
    for sample in log.samples or []:
        if not sample.scores:
            continue
        name = next(iter(sample.scores))
        score = sample.scores[name]
        meta = score.metadata or {}
        probe_id = _probe_id_of(sample)
        oracle_kind = _oracle_kind_of(score.explanation or "", probe_id, oracle_by_probe)

        # binary_hit: prefer the explicit metadata key; else recover from the score value (legacy).
        if "binary_hit" in meta and meta.get("binary_hit") is not None:
            binary_hit = bool(meta.get("binary_hit"))
            binary_from_value = False
        else:
            binary_hit = bool(_as_float(score.value) >= 0.5)
            binary_from_value = True

        rows.append(
            TrialRow(
                log_file=log_name,
                probe_id=probe_id,
                sample_id=str(getattr(sample, "id", "") or ""),
                epoch=int(getattr(sample, "epoch", 1) or 1),
                oracle_kind=oracle_kind,
                binary_hit=binary_hit,
                judge_success=bool(meta.get("success")),
                judge_applied=meta.get("judge_applied") is True,
                judge_unverified=bool(meta.get("judge_unverified")),
                binary_from_value=binary_from_value,
                transcript=str(meta.get("transcript", ""))[:_STORE_TRANSCRIPT],
                tool_calls=str(meta.get("tool_calls", ""))[:_STORE_TOOLS],
                reply=str(meta.get("reply", ""))[:_STORE_REPLY],
            )
        )
    return rows


def _as_float(value) -> float:
    """Inspect score values can be float/int/bool/str ('C'/'I') — coerce to a 0/1-ish float for the
    legacy binary fallback. Unknown -> 0.0 (treated as a miss, never a spurious hit)."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("c", "correct", "true", "yes", "1", "1.0"):
            return 1.0
        try:
            return float(v)
        except ValueError:
            return 0.0
    return 0.0


def parse_time(value: str | None) -> float | None:
    """Parse a `--since`/`--until` bound into an epoch-seconds float (or None when unset).

    Accepts an ISO date/datetime (`2026-06-30`, `2026-06-30T09:00:00`) — interpreted in LOCAL time,
    matching file mtimes — or raw epoch seconds. Raises ``ValueError`` on an unparseable value."""
    if value is None or value == "":
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return float(value)  # epoch seconds; raises ValueError if this is garbage too


def in_window(mtime: float, since_ts: float | None, until_ts: float | None) -> bool:
    """True iff ``mtime`` is within [since, until] (inclusive). Unset bounds are open. Pure."""
    if since_ts is not None and mtime < since_ts:
        return False
    if until_ts is not None and mtime > until_ts:
        return False
    return True


def iter_log_paths(
    globs: Iterable[str], *, exclude_globs: Iterable[str] = (), newest_first: bool = True,
    since_ts: float | None = None, until_ts: float | None = None,
) -> list[Path]:
    """Resolve `*.eval` paths from one or more globs, minus any matched by `exclude_globs`, deduped.

    Sorted newest-first by mtime by default. Used to target a SPECIFIC log set (e.g.
    `logs/2026-06-29T13-*.eval`) and explicitly EXCLUDE an in-flight run (e.g. `logs/2026-06-30T*`).
    `since_ts`/`until_ts` (epoch seconds) further restrict by file mtime — the robust way to isolate
    ONE run's fresh logs from the thousands of older ones that share the default `logs/` dir."""
    excluded: set[Path] = set()
    for pattern in exclude_globs:
        excluded.update(_glob(pattern))
    seen: dict[Path, None] = {}
    for pattern in globs:
        for p in _glob(pattern):
            if p not in excluded and in_window(p.stat().st_mtime, since_ts, until_ts):
                seen[p] = None
    paths = list(seen)
    paths.sort(key=lambda p: p.stat().st_mtime, reverse=newest_first)
    return paths


def _glob(pattern: str) -> list[Path]:
    """Glob a pattern that may be absolute or workspace-relative; matches `*.eval` files only."""
    p = Path(pattern)
    if p.is_absolute():
        return [m for m in Path(p.anchor).glob(str(p.relative_to(p.anchor))) if m.suffix == ".eval"]
    return [m for m in Path().glob(pattern) if m.suffix == ".eval"]
