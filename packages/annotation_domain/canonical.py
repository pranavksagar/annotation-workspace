"""Canonical JSON used for request hashes and training-snapshot checksums."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

COORDINATE_DECIMALS = 6


def round_coordinate(value: float) -> float:
    rounded = round(float(value), COORDINATE_DECIMALS)
    return 0.0 if rounded == 0 else rounded  # no negative zero in hashes


def _check(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and Infinity are not allowed in canonical JSON")
    if isinstance(value, dict):
        for item in value.values():
            _check(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check(item)
    return value


def canonical_dumps(value: Any) -> str:
    """Sorted keys, no insignificant whitespace, UTF-8 characters preserved."""
    return json.dumps(_check(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()
