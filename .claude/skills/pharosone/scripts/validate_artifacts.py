#!/usr/bin/env python3
"""Zero-dependency validator for pharosone onboarding artifacts.

The JSON schemas under ``pharosone/schemas/`` are the single source of truth for the
STRUCTURE of the ``passport.json`` and ``seams.json`` artifacts. This validator reads
those schemas at runtime and mechanically checks an artifact against them (types, enums,
``additionalProperties: false``, string patterns / channel grammar, capability vocabulary),
then layers a handful of SEMANTIC cross-field invariants that a plain schema cannot express.

Only the Python standard library is used (no jsonschema / third-party packages), mirroring
the hand-rolled schema+validator split used by the Cloudflare ``security-audit`` skill.

Mechanical checks (driven entirely by the schema file, never hard-coded here):
  * ``type`` (object / array / string / integer / boolean / number)
  * ``enum`` (topology base members, technique, capability vocabulary, canonical channels)
  * ``pattern`` (topology composite grammar, integration name:kind, seam channel grammar)
  * ``minLength`` / ``minItems``
  * ``required`` and ``additionalProperties: false``
  * ``$ref`` into local ``$defs``

Semantic invariants (hand-coded cross-field rules, kept out of the schema on purpose):
  * passport: ``channels`` and ``blind_spots`` must be disjoint (a channel cannot be both
    declared-routable and a blind spot).
  * seams: exactly one seam must have ``recommended: true``.
  * seams: every seam's ``narrowness`` must be within 1..5.
  * seams: the recommended seam must declare at least one injectable channel (a recommended
    seam that can inject nothing is a false recommendation).

CLI:
    python validate_artifacts.py passport <path>
    python validate_artifacts.py seams <path>

``<path>`` is either a ``.json`` file (parsed whole) or a markdown file with an embedded
```json fenced block (the machine block inside ``PASSPORT.md`` / ``SEAMS.md``). Exit code is
0 when valid and 1 when invalid; concrete field-path errors are printed to stderr.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_SCHEMA_FILES = {"passport": "passport.schema.json", "seams": "seams.schema.json"}

# Match the FIRST ```json fenced block inside a markdown file (the artifact's machine block).
_JSON_FENCE = re.compile(r"```[ \t]*json[^\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


class ArtifactError(Exception):
    """Raised when an artifact cannot be located or parsed (before schema validation)."""


def load_schema(kind: str) -> dict[str, Any]:
    """Read and parse a schema file by artifact kind."""
    schema_path = SCHEMA_DIR / _SCHEMA_FILES[kind]
    return json.loads(schema_path.read_text(encoding="utf-8"))


def load_artifact(path: Path) -> Any:
    """Load the artifact JSON from a ``.json`` file or a markdown ```json block."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        raw = text
    else:
        match = _JSON_FENCE.search(text)
        if match is None:
            raise ArtifactError(f"{path}: no ```json ...``` block found in markdown")
        raw = match.group(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path}: embedded JSON is not valid: {exc}") from exc


def _type_name(value: Any) -> str:
    """Schema-style name of a Python value's JSON type (for error messages)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _type_ok(value: Any, expected: str) -> bool:
    """Whether a value matches a JSON-schema ``type`` (bool is never int/number)."""
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True


def _join(path: str, key: Any) -> str:
    """Extend a JSON path with an object key."""
    return f"{path}.{key}" if path else str(key)


def _resolve_ref(root: dict[str, Any], ref: str) -> dict[str, Any]:
    """Resolve a local ``#/...`` JSON pointer against the root schema."""
    if not ref.startswith("#/"):
        raise ArtifactError(f"unsupported $ref (only local #/ pointers): {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def _validate_node(
    value: Any, schema: dict[str, Any], path: str, root: dict[str, Any], errors: list[str]
) -> None:
    """Validate one value against one (sub)schema, appending errors in place."""
    while "$ref" in schema:
        schema = _resolve_ref(root, schema["$ref"])

    where = path or "<root>"
    expected_type = schema.get("type")
    if expected_type is not None and not _type_ok(value, expected_type):
        errors.append(f"{where}: expected type '{expected_type}', got '{_type_name(value)}'")
        return

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{where}: value {value!r} is not one of {schema['enum']}")

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            errors.append(f"{where}: value {value!r} does not match pattern {pattern!r}")
        min_length = schema.get("minLength")
        if min_length is not None and len(value) < min_length:
            errors.append(f"{where}: string is shorter than minLength {min_length}")

    if expected_type == "object" and isinstance(value, dict):
        _validate_object(value, schema, path, root, errors)
    elif expected_type == "array" and isinstance(value, list):
        _validate_array(value, schema, path, root, errors)


def _validate_object(
    value: dict[str, Any], schema: dict[str, Any], path: str, root: dict[str, Any], errors: list[str]
) -> None:
    """Validate object required keys, additionalProperties, and known properties."""
    props: dict[str, Any] = schema.get("properties", {})
    for required_key in schema.get("required", []):
        if required_key not in value:
            errors.append(f"{_join(path, required_key)}: required property is missing")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in props:
                errors.append(
                    f"{_join(path, key)}: additional property not allowed "
                    "(additionalProperties: false)"
                )
    for key, subschema in props.items():
        if key in value:
            _validate_node(value[key], subschema, _join(path, key), root, errors)


def _validate_array(
    value: list[Any], schema: dict[str, Any], path: str, root: dict[str, Any], errors: list[str]
) -> None:
    """Validate array minItems and every item against the item schema."""
    min_items = schema.get("minItems")
    if min_items is not None and len(value) < min_items:
        where = path or "<root>"
        errors.append(f"{where}: array has {len(value)} item(s), fewer than minItems {min_items}")
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, element in enumerate(value):
            item_path = f"{path}[{index}]" if path else f"[{index}]"
            _validate_node(element, item_schema, item_path, root, errors)


def _semantic_passport(instance: Any, errors: list[str]) -> None:
    """Passport cross-field invariants not expressible in the schema."""
    if not isinstance(instance, dict):
        return
    channels = instance.get("channels")
    blind_spots = instance.get("blind_spots")
    if isinstance(channels, list) and isinstance(blind_spots, list):
        declared = {c for c in channels if isinstance(c, str)}
        blind = {b for b in blind_spots if isinstance(b, str)}
        overlap = sorted(declared & blind)
        if overlap:
            errors.append(
                "semantic: channels and blind_spots must be disjoint "
                f"(a channel cannot be both routable and a blind spot); overlap: {overlap}"
            )


def _semantic_seams(seams: Any, errors: list[str]) -> None:
    """Seams cross-field invariants not expressible in the schema."""
    if not isinstance(seams, list):
        return
    recommended_indices: list[int] = []
    for index, seam in enumerate(seams):
        if not isinstance(seam, dict):
            continue
        narrowness = seam.get("narrowness")
        if isinstance(narrowness, int) and not isinstance(narrowness, bool):
            if narrowness < 1 or narrowness > 5:
                errors.append(
                    f"semantic: seams[{index}].narrowness {narrowness} is outside "
                    "the allowed range 1..5"
                )
        if seam.get("recommended") is True:
            recommended_indices.append(index)
    if len(recommended_indices) != 1:
        errors.append(
            "semantic: exactly one seam must have recommended=true, "
            f"found {len(recommended_indices)} at indices {recommended_indices}"
        )
    else:
        recommended = seams[recommended_indices[0]]
        channels = recommended.get("channels") if isinstance(recommended, dict) else None
        if not (isinstance(channels, list) and len(channels) > 0):
            errors.append(
                f"semantic: the recommended seam (index {recommended_indices[0]}) must "
                "declare at least one channel (a recommended seam that injects nothing "
                "is false coverage)"
            )


def validate_passport(instance: Any) -> list[str]:
    """Validate a passport instance mechanically (schema) then semantically."""
    schema = load_schema("passport")
    errors: list[str] = []
    _validate_node(instance, schema, "", schema, errors)
    _semantic_passport(instance, errors)
    return errors


def validate_seams(instance: Any) -> list[str]:
    """Validate a seams instance mechanically (schema) then semantically.

    The on-disk artifact is a bare array of seam objects; a ``{"seams": [...]}`` wrapper is
    also accepted for forward-compatibility.
    """
    schema = load_schema("seams")
    if isinstance(instance, dict) and "seams" in instance:
        seams = instance["seams"]
    else:
        seams = instance
    errors: list[str] = []
    _validate_node(seams, schema, "", schema, errors)
    _semantic_seams(seams, errors)
    return errors


def validate(kind: str, instance: Any) -> list[str]:
    """Dispatch validation by artifact kind."""
    if kind == "passport":
        return validate_passport(instance)
    return validate_seams(instance)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns an exit code (0 valid, 1 invalid / error)."""
    parser = argparse.ArgumentParser(
        description=(
            "Validate a pharosone onboarding artifact against its JSON schema "
            "plus semantic invariants."
        )
    )
    parser.add_argument("kind", choices=["passport", "seams"], help="artifact kind")
    parser.add_argument(
        "path",
        help="path to a .json artifact or a .md file with an embedded ```json block",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    try:
        instance = load_artifact(path)
    except ArtifactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    errors = validate(args.kind, instance)
    if errors:
        print(
            f"INVALID: {path} ({args.kind}) — {len(errors)} problem(s):",
            file=sys.stderr,
        )
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print(f"OK: {path} ({args.kind}) is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
