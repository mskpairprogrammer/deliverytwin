"""Chat-completion client for the AI features (standard library only).

Backends, in order of precedence: recorded sample (explicit demo mode), Databricks Model Serving, Microsoft
Foundry, then OpenAI (OPENAI_API_KEY). On Azure App Service only Foundry (and the recorded demo) is used: the
OpenAI key and Model Serving are for local runs. With none configured, callers fall back to rules-based results.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .integrations import azure_app_service, databricks_switch, foundry_switch

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
            name, value = name.strip(), value.strip()
            if value[:1] in ('"', "'") and value[0] in value[1:]:
                value = value[1:value.index(value[0], 1)]  # quoted: keep everything inside, including '#'
            else:
                value = re.split(r"\s#", value, maxsplit=1)[0].strip()  # unquoted: ' # ...' starts a comment
                value = "" if value.startswith("#") else value
            if name and name not in os.environ:
                os.environ[name] = value


load_env_file()


def ai_mode() -> str:
    """Which backend answers AI requests.

    Priority: recorded (explicit demo override), Databricks Model Serving, Microsoft Foundry, OpenAI, else off.
    DELIVERYTWIN_DATABRICKS_ENABLED=0 and DELIVERYTWIN_FOUNDRY_ENABLED=0 skip their backends (see integrations.py).
    On Azure App Service only Foundry models run: OPENAI_API_KEY and Databricks Model Serving are ignored there.
    """
    if os.environ.get("DELIVERYTWIN_AI_MODE", "").strip().lower() == "recorded":
        return "recorded"
    cloud = azure_app_service()
    if not cloud and databricks_serving_settings():
        return "databricks_serving"
    if foundry_settings():
        return "foundry"
    return "openai" if not cloud and os.environ.get("OPENAI_API_KEY", "").strip() else "off"


SERVING_ENDPOINT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def databricks_serving_settings() -> dict[str, str] | None:
    """A Databricks Model Serving chat endpoint, from DATABRICKS_SERVING_ENDPOINT (or COPILOT_AI_ENDPOINT).

    Needs the workspace host and either a token or OAuth client credentials (as Databricks Apps provide).
    """
    if databricks_switch() is False:
        return None
    endpoint = (os.environ.get("DATABRICKS_SERVING_ENDPOINT") or os.environ.get("COPILOT_AI_ENDPOINT") or "").strip()
    host = os.environ.get("DATABRICKS_HOST", "").strip().replace("https://", "").rstrip("/")
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    has_oauth = bool(os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_CLIENT_SECRET"))
    if not endpoint or not host or "/" in host or not SERVING_ENDPOINT_NAME.match(endpoint) or not (token or has_oauth):
        return None
    return {"endpoint": endpoint, "host": host, "token": token}


FOUNDRY_PROJECT = re.compile(r"^https://([a-z0-9][a-z0-9-]{0,62})\.services\.ai\.azure\.com/api/projects/[^/]+/?$", re.I)


def _is_foundry_models_endpoint(endpoint: str) -> bool:
    return (urlsplit(endpoint).hostname or "").lower().endswith(".services.ai.azure.com")


def foundry_endpoint() -> str:
    """The Foundry resource's model endpoint: FOUNDRY_ENDPOINT, else derived from FOUNDRY_PROJECT_ENDPOINT
    (https://<resource>.services.ai.azure.com/api/projects/<p> -> https://<resource>.services.ai.azure.com)."""
    explicit = os.environ.get("FOUNDRY_ENDPOINT", "").strip().rstrip("/")
    if explicit:
        return explicit
    project = FOUNDRY_PROJECT.match(os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").strip())
    return f"https://{project.group(1)}.services.ai.azure.com" if project else ""


def foundry_settings() -> dict[str, str] | None:
    """The Microsoft Foundry model deployment (FOUNDRY_MODEL); off when DELIVERYTWIN_FOUNDRY_ENABLED=0."""
    if foundry_switch() is False:
        return None
    endpoint = foundry_endpoint()
    deployment = os.environ.get("FOUNDRY_MODEL", "").strip()
    if not endpoint or not deployment or not endpoint.startswith("https://"):
        return None
    models_endpoint = _is_foundry_models_endpoint(endpoint)
    return {
        "endpoint": endpoint, "deployment": deployment,
        "api_version": os.environ.get("FOUNDRY_API_VERSION", "").strip()
        or ("2024-05-01-preview" if models_endpoint else "2024-10-21"),
        "api_key": os.environ.get("FOUNDRY_API_KEY", "").strip(),
    }


def openai_configured() -> bool:
    return ai_mode() != "off"


def openai_model() -> str:
    mode = ai_mode()
    if mode == "recorded":
        return "recorded-sample"
    if mode == "databricks_serving":
        return databricks_serving_settings()["endpoint"]
    if mode == "foundry":
        return foundry_settings()["deployment"]
    return os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL


def _rejects_temperature(error: urllib.error.HTTPError) -> bool:
    """Reasoning models (the GPT-5 family and later) accept only the default temperature and answer HTTP 400."""
    if error.code != 400:
        return False
    try:
        detail = error.read().decode("utf-8", "replace")
    except Exception:  # no readable body
        return False
    return "temperature" in detail.lower()


def _post_chat(url: str, headers: dict[str, str], payload: dict[str, Any]) -> str:
    """POST a chat-completion request and return the reply text.

    A model that rejects ``temperature`` is asked once more without it, so the deterministic setting is used
    wherever the model allows it.
    """
    def post(body: dict[str, Any]) -> str:
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            reply = json.load(response)
        return str(reply["choices"][0]["message"]["content"] or "").strip()

    try:
        return post(payload)
    except urllib.error.HTTPError as error:
        if "temperature" not in payload or not _rejects_temperature(error):
            raise
    return post({name: value for name, value in payload.items() if name != "temperature"})


def _complete_openai(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise LLMError("OPENAI_API_KEY is not set.")
    payload: dict[str, Any] = {"model": openai_model(), "messages": messages, "temperature": 0}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    try:
        return _post_chat(OPENAI_URL, headers, payload)
    except urllib.error.HTTPError as error:
        raise LLMError(f"OpenAI request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        raise LLMError("OpenAI request failed.") from error


def _foundry_token() -> str:
    try:
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential().get_token("https://cognitiveservices.azure.com/.default").token
    except Exception as error:  # any credential failure is reported as a request failure
        raise LLMError("Microsoft Foundry sign-in failed.") from error


def _complete_foundry(settings: dict[str, str], messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    models_endpoint = _is_foundry_models_endpoint(settings["endpoint"])
    payload: dict[str, Any] = {"messages": messages}
    if models_endpoint:
        payload["model"] = settings["deployment"]
    else:
        payload["temperature"] = 0
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if settings["api_key"]:
        headers["api-key"] = settings["api_key"]
    else:
        headers["Authorization"] = "Bearer " + _foundry_token()
    if models_endpoint:
        url = f"{settings['endpoint']}/models/chat/completions?api-version={settings['api_version']}"
    else:
        url = (
            f"{settings['endpoint']}/openai/deployments/{settings['deployment']}/chat/completions"
            f"?api-version={settings['api_version']}"
        )
    try:
        return _post_chat(url, headers, payload)
    except urllib.error.HTTPError as error:
        raise LLMError(f"Microsoft Foundry request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        raise LLMError("Microsoft Foundry request failed.") from error


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
    payload: dict[str, Any] = {"messages": messages, "temperature": 0}
    try:
        if json_mode:
            try:
                return _post_chat(url, headers, {**payload, "response_format": {"type": "json_object"}})
            except urllib.error.HTTPError as error:
                if error.code != 400:  # some serving endpoints reject response_format; the prompts still ask for JSON
                    raise
        return _post_chat(url, headers, payload)
    except urllib.error.HTTPError as error:
        raise LLMError(f"Databricks Model Serving request failed with HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as error:
        raise LLMError("Databricks Model Serving request failed.") from error


def complete(messages: list[dict[str, str]], *, json_mode: bool = False) -> str:
    """Answer with the backend ai_mode() picks: recorded, Databricks Model Serving, Foundry or OpenAI."""
    mode = ai_mode()
    if mode == "recorded":
        from .recorded_ai import RecordedError, recorded_complete

        try:
            return recorded_complete(messages, json_mode=json_mode)
        except RecordedError as error:
            raise LLMError(str(error)) from error
    if mode == "databricks_serving":
        return _complete_databricks(databricks_serving_settings(), messages, json_mode=json_mode)
    if mode == "foundry":
        return _complete_foundry(foundry_settings(), messages, json_mode=json_mode)
    if mode == "openai":
        return _complete_openai(messages, json_mode=json_mode)
    raise LLMError("No AI model is configured." + (" On Azure App Service only Foundry models are used." if azure_app_service() else ""))


def ai_status() -> dict[str, Any]:
    mode = ai_mode()
    labels = {
        "recorded": "Recorded sample answers: no live model is being called.",
        "databricks_serving": "Databricks Model Serving",
        "foundry": "Microsoft Foundry",
        "openai": "OpenAI",
        "off": "No AI model configured: rules-based fallbacks are shown.",
    }
    from .integrations import describe

    return {"mode": mode, "model": openai_model() if mode != "off" else None, "label": labels[mode],
            "integrations": describe()}
