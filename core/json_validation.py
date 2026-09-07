"""Strict JSON decoding for operator-supplied configuration and mutations."""

from __future__ import annotations

import json
import math
from typing import Any


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON contains a duplicate object key.")
        result[key] = value
    return result


def _finite_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON contains a non-finite number.")
    return number


def loads_strict_json(value: str) -> Any:
    return json.loads(
        value, object_pairs_hook=_unique_object,
        parse_float=_finite_number, parse_constant=_finite_number,
    )
