import pytest
from pydantic import ValidationError

from probe_engine.domain.framework import (
    Control,
    DensityThreshold,
    Framework,
    Verification,
)


def _framework() -> Framework:
    return Framework(
        id="aiuc-1",
        version="v1",
        name="AIUC-1",
        controls=[
            Control(
                id="B001",
                category="B",
                title="Third-party testing of adversarial robustness",
                mandatory=True,
                density_threshold=DensityThreshold(min=5, max=7),
                verification=Verification(title="verified@aiuc-1.com"),
            ),
            Control(
                id="B008",
                category="B",
                title="Protect AI system deployment environment",
                behaviorally_testable=False,
            ),
        ],
    )


def test_get_control_found_and_missing():
    fw = _framework()
    assert fw.get_control("B001").title.startswith("Third-party")
    assert fw.get_control("ZZZ") is None


def test_control_ids():
    assert _framework().control_ids() == {"B001", "B008"}


def test_density_threshold_rejects_inverted_range():
    with pytest.raises(ValidationError):
        DensityThreshold(min=7, max=5)


def test_verification_defaults_to_unverified_wording():
    assert Verification(title="verified@aiuc-1.com").wording == "unverified"


def test_non_testable_control_has_no_threshold_requirement():
    fw = _framework()
    assert fw.get_control("B008").behaviorally_testable is False
