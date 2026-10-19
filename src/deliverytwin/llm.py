"""Chat-completion client for the AI features (standard library only).

Backends, in order of precedence: recorded sample (explicit demo mode), Databricks Model Serving, Azure
OpenAI, then OpenAI (OPENAI_API_KEY). With none configured, callers fall back to rules-based results.
"""
from __future__ import annotations

import json
import os
import re
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
    """Which backend answers AI requests.

    Priority: recorded (explicit demo override), Databricks Model Serving, Azure OpenAI, OpenAI, else off.
    """
    if os.environ.get("DELIVERYTWIN_AI_MODE", "").strip().lower() == "recorded":
        return "recorded"
    if databricks_serving_settings():
        return "databricks_serving"
    if azure_settings():
        return "azure_openai"
    return "openai" if os.environ.get("OPENAI_API_KEY", "").strip() else "off"


SERVING_ENDPOINT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def databricks_serving_settings() -> dict[str, str] | None:
    """A Databricks Model Serving chat endpoint, from DATABRICKS_SERVING_ENDPOINT (or COPILOT_AI_ENDPOINT).

    Needs the workspace host and either a token or OAuth client credentials (as Databricks Apps provide).
    """
    endpoint = (os.environ.get("DATABRICKS_SERVING_ENDPOINT") or os.environ.get("COPILOT_AI_ENDPOINT") or "").strip()
    host = os.environ.get("DATABRICKS_HOST", "").strip().replace("https://", "").rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    has_oauth = bool(os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_CLIENT_SECRET"))
    if not endpoint or not host or "/" in host or not SERVING_ENDPOINT_NAME.match(endpoint) or not (token or has_oauth):
        return None
    return {"endpoint": endpoint, "host": host, "token": token}


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
    if mode == "databricks_serving":
        return databricks_serving_settings()["endpoint"]
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


def _databricks_headers(settings: dict[str, str]) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if settings["token"]:
        headers["Authorization"] = "Bearer " + settings["token"]
        return headers
    try:  # OAuth machine-to-machine credentials, as injected into Databricks Apps
        from databricks.sdk.core import Config

        headers.update(Config(host=f"https://{settings['host']}").authenticate())
    except Exception as error:
        raise LLMError("Databricks sign-in failed.") from error
    return headers


def _complete_databricks(settings: dict[str, str], messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    """Call a Databricks Model Serving chat endpoint (OpenAI-compatible request and response)."""
    url = f"https://{settings['host']}/serving-endpoints/{settings['endpoint']}/invocations"
    headers = _databricks_headers(settings)

    def post(payload: dict[str, Any]) -> str:
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.load(response)
        return str(body["choices"][0]["message"]["content"] or "").strip()

    payload: dict[str, Any] = {"messages": messages, "temperature": 0}
    try:
        if json_mode:
            try:
                return post({**payload, "response_format": {"type": "json_object"}})
            except urllib.error.HTTPError as error:
                if error.code != 400:  # some serving endpoints reject response_format; the prompts still ask for JSON
                    raise
        return post(payload)
    except urllib.error.HTTPError as error:
        raise LLMError(f"Databricks Model Serving request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        raise LLMError("Databricks Model Serving request failed.") from error


def complete(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    """Answer with the recorded sample, Databricks Model Serving, Azure OpenAI, or OpenAI, in that order."""
    mode = ai_mode()
    if mode == "recorded":
        from .recorded_ai import RecordedError, recorded_complete

        try:
            return recorded_complete(messages, json_mode=json_mode)
        except RecordedError as error:
            raise LLMError(str(error)) from error
    serving = databricks_serving_settings()
    if serving:
        return _complete_databricks(serving, messages, json_mode=json_mode)
    settings = azure_settings()
    if settings:
        return _complete_azure(settings, messages, json_mode=json_mode)
    return _complete_openai(messages, json_mode=json_mode)


def ai_status() -> dict[str, Any]:
    mode = ai_mode()
    labels = {
        "recorded": "Recorded sample answers: no live model is being called.",
        "databricks_serving": "Databricks Model Serving",
        "azure_openai": "Azure OpenAI",
        "openai": "OpenAI",
        "off": "No AI model configured: rules-based fallbacks are shown.",
    }
    return {"mode": mode, "model": openai_model() if mode != "off" else None, "label": labels[mode]}
