"""Load framework (standard) and crosswalk YAML (spec §5.6, §12)."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from probe_engine.domain.crosswalk import Crosswalk
from probe_engine.domain.framework import Framework


class MappingError(Exception):
    def __init__(self, message: str, path: str | None = None):
        super().__init__(f"{path}: {message}" if path else message)
        self.path = path


def _read_yaml(path: str | Path) -> dict:
    path = Path(path)
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MappingError(f"invalid YAML: {exc}", str(path)) from exc


def load_framework(path: str | Path) -> Framework:
    try:
        return Framework.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise MappingError(f"schema error: {exc}", str(path)) from exc


def load_crosswalk(path: str | Path) -> Crosswalk:
    try:
        return Crosswalk.model_validate(_read_yaml(path))
    except ValidationError as exc:
        raise MappingError(f"schema error: {exc}", str(path)) from exc
