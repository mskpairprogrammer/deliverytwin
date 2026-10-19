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


def ai_mode() -> str:
    """Which backend answers AI requests: recorded, azure_openai, openai, or off."""
    if os.environ.get("DELIVERYTWIN_AI_MODE", "").strip().lower() == "recorded":
        return "recorded"
    if azure_settings():
        return "azure_openai"
    return "openai" if os.environ.get("OPENAI_API_KEY", "").strip() else "off"


def azure_settings() -> dict[str, str] | None:
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    if not endpoint or not deployment or not endpoint.startswith("https://"):
        return None
    return {
        "endpoint": endpoint, "deployment": deployment,
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "").strip() or "2024-10-21",
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY", "").strip(),
    }


def openai_configured() -> bool:
    return ai_mode() != "off"


def openai_model() -> str:
    mode = ai_mode()
    if mode == "recorded":
        return "recorded-sample"
    if mode == "azure_openai":
        return azure_settings()["deployment"]
    return os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL


def _complete_openai(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
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


def _azure_token() -> str:
    try:
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    except Exception as error:  # any credential failure is reported as a request failure
        raise LLMError("Azure OpenAI sign-in failed.") from error


def _complete_azure(settings: dict[str, str], messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    payload: dict[str, Any] = {"messages": messages, "temperature": 0}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["api-key"] = settings["api_key"]
    else:
        headers["Authorization"] = "Bearer " + _azure_token()
    url = (
        f"{settings['endpoint']}/openai/deployments/{settings['deployment']}/chat/completions"
        f"?api-version={settings['api_version']}"
    )
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
        return str(body["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as error:
        raise LLMError(f"Azure OpenAI request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        raise LLMError("Azure OpenAI request failed.") from error


def complete(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    """Answer with the recorded sample, Azure OpenAI, or OpenAI, in that order of precedence."""
    mode = ai_mode()
    if mode == "recorded":
        from .recorded_ai import RecordedError, recorded_complete

        try:
            return recorded_complete(messages, json_mode=json_mode)
        except RecordedError as error:
            raise LLMError(str(error)) from error
    settings = azure_settings()
    if settings:
        return _complete_azure(settings, messages, json_mode=json_mode)
    return _complete_openai(messages, json_mode=json_mode)


def ai_status() -> dict[str, Any]:
    mode = ai_mode()
    labels = {
        "recorded": "Recorded sample answers: no live model is being called.",
        "azure_openai": "Azure OpenAI",
        "openai": "OpenAI",
        "off": "No AI model configured: rules-based fallbacks are shown.",
    }
    return {"mode": mode, "model": openai_model() if mode != "off" else None, "label": labels[mode]}
