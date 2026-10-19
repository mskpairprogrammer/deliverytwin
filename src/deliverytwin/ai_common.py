"""Shared helpers for AI features whose output is validated before it reaches the UI."""
from __future__ import annotations

import json
import math
from typing import Any

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
