"""Master switches for the optional cloud integrations: Azure Databricks and Microsoft Foundry.

Each switch is an environment variable read on every check:

* ``1`` / ``true`` / ``yes`` / ``on``: enabled. The integration is used wherever its settings are filled in.
* ``0`` / ``false`` / ``no`` / ``off``: disabled. Its settings are ignored even when they are present.
* unset or blank: automatic, the behaviour before the switches existed (used whenever its settings are present).

With both disabled the app runs entirely on DuckDB with OpenAI (``OPENAI_API_KEY``, local runs only) or the
rules-based fallback. On Azure App Service the only model backend is Foundry.
"""
from __future__ import annotations

import os
from typing import Mapping

DATABRICKS_FLAG = "DELIVERYTWIN_DATABRICKS_ENABLED"
FOUNDRY_FLAG = "DELIVERYTWIN_FOUNDRY_ENABLED"
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def switch(name: str, env: Mapping[str, str] | None = None) -> bool | None:
    """True or False when the switch is set, None when it is unset (automatic)."""
    value = (os.environ if env is None else env).get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    if value:
        raise ValueError(f"{name} must be 1 or 0 (true/false, yes/no, on/off), not {value!r}.")
    return None


def databricks_switch(env: Mapping[str, str] | None = None) -> bool | None:
    return switch(DATABRICKS_FLAG, env)


def foundry_switch(env: Mapping[str, str] | None = None) -> bool | None:
    return switch(FOUNDRY_FLAG, env)


def azure_app_service(env: Mapping[str, str] | None = None) -> bool:
    """True inside Azure App Service, which sets WEBSITE_SITE_NAME. There only Foundry models are used."""
    return bool((os.environ if env is None else env).get("WEBSITE_SITE_NAME", "").strip())


def describe(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """`on`, `off` or `auto` per integration, for status output."""
    label = {True: "on", False: "off", None: "auto"}
    return {"databricks": label[databricks_switch(env)], "foundry": label[foundry_switch(env)]}
