"""Load and validate the versioned probe corpus (spec §2.2)."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from probe_engine.domain.probe import Probe


class CorpusError(Exception):
    """Raised when a probe file is invalid or the corpus has conflicts."""

    def __init__(self, message: str, path: str | None = None):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


def load_probe_file(path: str | Path) -> Probe:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CorpusError(f"invalid YAML: {exc}", str(path)) from exc
    try:
        return Probe.model_validate(data)
    except ValidationError as exc:
        raise CorpusError(f"schema error: {exc}", str(path)) from exc


def load_corpus(probes_dir: str | Path) -> list[Probe]:
    probes_dir = Path(probes_dir)
    probes: list[Probe] = []
    seen: dict[str, str] = {}
    for path in sorted(probes_dir.glob("*.yaml")):
        probe = load_probe_file(path)
        if probe.id in seen:
            raise CorpusError(
                f"duplicate probe id {probe.id!r} (also in {seen[probe.id]})", str(path)
            )
        seen[probe.id] = str(path)
        probes.append(probe)
    return probes
