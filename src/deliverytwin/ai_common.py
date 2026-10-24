"""Shared helpers for AI features whose output is validated before it reaches the UI."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable

from .llm import LLMError, complete, openai_configured


def llm_json(messages: list[dict[str, str]]) -> tuple[Any, dict[str, str] | None]:
    """Return (parsed JSON, None) or (None, reason) where reason explains why AI was not used."""
    if not openai_configured():
        return None, {"code": "not_configured"}
    try:
        return json.loads(complete(messages, json_mode=True)), None
    except LLMError as error:
        return None, {"code": "request_failed", "detail": str(error)}
    except ValueError:
        return None, {"code": "invalid_response", "detail": "The reply was not valid JSON."}


INVALID_RESPONSE = {"code": "invalid_response", "detail": "The AI response did not pass validation."}

# A figure stands alone: digits glued to letters or after an identifier's hyphen (P80, HIST-001) are not figures.
_NUMBER = re.compile(r"(?<![A-Za-z\d.])(?<![A-Za-z\d]-)\d[\d,]*(?:\.\d+)?")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


def _collect_numbers(value: object, out: set[float]) -> None:
    """Every number the AI may quote from ``value``: its numeric fields (as given, whole and to one decimal),
    numbers written inside its strings, the day, month and year of ISO dates, and list lengths."""
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        if math.isfinite(value):
            magnitude = abs(float(value))  # "down 50 points" quotes a change of -50
            out.update({magnitude, float(round(magnitude)), round(magnitude, 1)})
        return
    if isinstance(value, str):
        for year, month, day in _ISO_DATE.findall(value):
            out.update({float(year), float(month), float(day)})
        for match in _NUMBER.findall(value):
            try:
                out.add(float(match.replace(",", "")))
            except ValueError:
                pass
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, out)
        return
    if isinstance(value, (list, tuple, set)):
        out.add(float(len(value)))
        for item in value:
            _collect_numbers(item, out)


def unsupported_numbers(texts: Iterable[str | None], *sources: object) -> list[str]:
    """Numbers written in the AI's ``texts`` that appear nowhere in ``sources`` (the computed data it was given
    and its own validated structured answer). An empty list means the number check passed."""
    allowed: set[float] = set()
    for source in sources:
        _collect_numbers(source, allowed)
    unmatched: list[str] = []
    for value in texts:
        for match in _NUMBER.findall(value or ""):
            try:
                number = float(match.replace(",", ""))
            except ValueError:
                continue
            if number not in allowed and match not in unmatched:
                unmatched.append(match)
    return unmatched


def number_check_failure(unmatched: list[str]) -> dict[str, str]:
    return {
        "code": "number_check",
        "detail": "The AI wrote numbers that are not in the computed results: " + ", ".join(unmatched[:4]) + ".",
    }


def text(value: object, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def text_list(value: object, items: int, limit: int) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return [item.strip()[:limit] for item in value if item.strip()][:items]


def whole(value: object, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    rounded = round(value)
    return int(rounded) if low <= rounded <= high else None
