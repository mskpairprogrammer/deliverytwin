"""Minimal OpenAI Chat Completions client (standard library only).

The API key is read only from the OPENAI_API_KEY environment variable.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
TIMEOUT_SECONDS = 30


class LLMError(RuntimeError):
    pass


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env without overriding existing variables.

    Without an explicit path, the app folder's .env is read first, then the repository root's.
    """
    app_dir = Path(__file__).resolve().parent.parent
    candidates = [path] if path else [app_dir / ".env", app_dir.parent / ".env"]
    for env_path in candidates:
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if name and name not in os.environ:
                os.environ[name] = value


load_env_file()


def openai_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL


def complete(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise LLMError("OPENAI_API_KEY is not set.")
    payload: dict[str, Any] = {"model": openai_model(), "messages": messages, "temperature": 0}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
        return str(body["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as error:
        raise LLMError(f"OpenAI request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        raise LLMError("OpenAI request failed.") from error
