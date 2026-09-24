"""Bounded local I/O. No network access, shell commands or archive extraction."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft202012Validator


class ProducerError(ValueError):
    """An actionable, expected validation or workflow failure."""


def reject_constant(value: str) -> None:
    raise ProducerError(f"Non-finite JSON number: {value}")


def finite_float(value: str) -> float:
    """Reject exponent overflow too; parse_constant does not catch 1e999."""
    result = float(value)
    if not math.isfinite(result):
        raise ProducerError(f"Non-finite JSON number: {value}")
    return result


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProducerError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def opportunity_schema_file(value: dict) -> str:
    """Choose opportunity schema filename from schema_version without reinterpreting v1 as v2."""
    version = value.get("schema_version")
    if version == 1:
        return "opportunity.schema.json"
    if version == 2:
        return "opportunity.v2.schema.json"
    raise ProducerError(f"Unsupported opportunity schema_version: {version!r}")


def load_json(path: Path, schema: str | None = None) -> dict:
    if path.stat().st_size > 2_000_000:
        raise ProducerError(f"JSON input too large: {path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           parse_constant=reject_constant, parse_float=finite_float,
                           object_pairs_hook=unique_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ProducerError(f"Invalid UTF-8 JSON: {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProducerError(f"Expected JSON object: {path.name}")
    if schema:
        if schema == "opportunity":
            schema_path = Path(__file__).parent / "schemas" / opportunity_schema_file(value)
        else:
            schema_path = Path(__file__).parent / "schemas" / f"{schema}.schema.json"
        validator = Draft202012Validator(load_json(schema_path))
        errors = sorted(validator.iter_errors(value), key=lambda e: str(list(e.path)))
        if errors:
            first = errors[0]
            location = ".".join(map(str, first.path)) or "root"
            raise ProducerError(f"{path.name}, {location}: {first.message}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_root(path: Path) -> Path:
    absolute = path.absolute()
    for part in (absolute, *absolute.parents):
        if part.is_symlink():
            raise ProducerError(f"Symlinked workspace/output is not allowed: {part}")
    if not absolute.is_dir():
        raise ProducerError(f"Directory not found: {absolute}")
    return absolute.resolve()


def local_path(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    """Only ordinary files below root; reject symlinks and traversal explicitly."""
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise ProducerError(f"Unsafe relative path: {relative!r}")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(p in ("", ".", "..") for p in relative.split("/")):
        raise ProducerError(f"Unsafe relative path: {relative!r}")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ProducerError(f"Symlinked input/output is not allowed: {relative}")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ProducerError(f"Path escapes workspace: {relative}")
    if must_exist and not current.is_file():
        raise ProducerError(f"Input file not found: {relative}")
    return current


def utc_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProducerError(f"Invalid ISO timestamp: {value}") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ProducerError(f"Timestamp needs an explicit UTC offset: {value}")
    return result.astimezone(timezone.utc)


def word_count(text: str) -> int:
    """Whitespace-delimited count; the organiser's portal may count differently."""
    return len(text.split())


def slug(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", value):
        raise ProducerError("Name must be 1–48 lowercase letters/digits/hyphens")
    return value
