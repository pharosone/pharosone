"""Local-model onboarding defaults are documented consistently across the skill copies.

Two guarantees:
  (a) the tracked `skills/<x>` and the on-disk `.claude/skills/<x>` copies are BYTE-IDENTICAL (the two
      trees must never drift — an operator's installed skill must match the repo's);
  (b) the Local defaults actually appear in the docs: PharosOne's own judge (`pharos-one/pharos-judge-free`)
      and the pinned Granite attacker (`granite-4.1`) — and the profile template STILL carries PR #1's
      `judge_batch_size` / `max_connections` run-safety knobs (regression guard against a doc rewrite
      silently dropping them)."""
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# The skill files this feature touched, relative to each skill tree root.
SKILL_FILES = [
    "deploy-local-model/SKILL.md",
    "build-run-profile/SKILL.md",
    "pharosone/SKILL.md",
    "pharosone/templates/profile_template.yaml",
]


def _pair(rel: str) -> tuple[Path, Path]:
    return REPO_ROOT / "skills" / rel, REPO_ROOT / ".claude" / "skills" / rel


@pytest.mark.parametrize("rel", SKILL_FILES)
def test_tracked_and_installed_skill_copies_are_byte_identical(rel: str):
    tracked, installed = _pair(rel)
    if not (tracked.exists() and installed.exists()):
        pytest.skip(f"{rel} not present in both skill trees")
    assert tracked.read_bytes() == installed.read_bytes(), (
        f"skills/{rel} and .claude/skills/{rel} have drifted — re-sync them byte-for-byte."
    )


@pytest.mark.parametrize(
    "rel", ["deploy-local-model/SKILL.md", "build-run-profile/SKILL.md",
            "pharosone/templates/profile_template.yaml"]
)
def test_local_defaults_named_in_docs(rel: str):
    text = (REPO_ROOT / "skills" / rel).read_text(encoding="utf-8")
    assert "pharos-one/pharos-judge-free" in text, f"{rel} must name the local judge repo"
    assert "granite-4.1" in text, f"{rel} must name the pinned Granite attacker"


def test_profile_template_still_has_pr1_run_safety_knobs():
    # Regression guard: the Local-default rewrite must NOT drop PR #1's run-safety knobs.
    text = (REPO_ROOT / "skills" / "pharosone" / "templates" / "profile_template.yaml").read_text(
        encoding="utf-8"
    )
    assert "judge_batch_size" in text
    assert "max_connections" in text
    # and the new logprobs verdict knobs are documented alongside them
    assert "judge_kind: logprobs" in text
    assert "judge_threshold: 0.68" in text
