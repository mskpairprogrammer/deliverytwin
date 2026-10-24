"""DELIVERYTWIN_DATABRICKS_ENABLED / DELIVERYTWIN_FOUNDRY_ENABLED and the .env loader that feeds them."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deliverytwin.chat import foundry_chat_settings
from deliverytwin.integrations import describe, switch
from deliverytwin.llm import ai_mode, foundry_settings, databricks_serving_settings, load_env_file
from deliverytwin.storage.connection import resolve_dialect

DATABRICKS = {
    "DATABRICKS_HOST": "https://adb-1.2.azuredatabricks.net", "DATABRICKS_TOKEN": "dapi-test",
    "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/abc", "DATABRICKS_SERVING_ENDPOINT": "chat-model",
}
FOUNDRY = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://foundry-res.services.ai.azure.com/api/projects/p", "FOUNDRY_MODEL": "gpt-4o-mini",
}
MANAGED = (*DATABRICKS, *FOUNDRY, "DATABRICKS_SERVER_HOSTNAME", "DELIVERYTWIN_DB_DIALECT", "DELIVERYTWIN_AI_MODE",
           "DELIVERYTWIN_DATABRICKS_ENABLED", "DELIVERYTWIN_FOUNDRY_ENABLED", "OPENAI_API_KEY", "COPILOT_AI_ENDPOINT",
           "FOUNDRY_ENDPOINT", "FOUNDRY_API_KEY", "WEBSITE_SITE_NAME")


def environment(**values: str) -> patch:
    clean = {key: value for key, value in os.environ.items() if key not in MANAGED}
    return patch.dict(os.environ, {**clean, **values}, clear=True)


class SwitchTests(unittest.TestCase):
    def test_switch_values(self) -> None:
        for raw, expected in (("1", True), ("on", True), ("TRUE", True), ("0", False), ("no", False), ("", None), ("  ", None)):
            self.assertEqual(switch("X", {"X": raw}), expected, raw)
        self.assertIsNone(switch("X", {}))
        with self.assertRaises(ValueError):
            switch("X", {"X": "maybe"})

    def test_disabled_integrations_ignore_their_settings(self) -> None:
        with environment(**DATABRICKS, **FOUNDRY, OPENAI_API_KEY="sk-test",
                         DELIVERYTWIN_DATABRICKS_ENABLED="0", DELIVERYTWIN_FOUNDRY_ENABLED="0"):
            self.assertEqual(resolve_dialect(), "duckdb")
            self.assertIsNone(databricks_serving_settings())
            self.assertIsNone(foundry_settings())
            self.assertIsNone(foundry_chat_settings())
            self.assertEqual(ai_mode(), "openai")
            self.assertEqual(describe(), {"databricks": "off", "foundry": "off"})

    def test_enabled_integrations_use_their_settings(self) -> None:
        with environment(**DATABRICKS, **FOUNDRY, DELIVERYTWIN_DATABRICKS_ENABLED="1", DELIVERYTWIN_FOUNDRY_ENABLED="1"):
            self.assertEqual(resolve_dialect(), "databricks")
            self.assertEqual(ai_mode(), "databricks_serving")  # Databricks Model Serving is tried before Foundry
        with environment(**FOUNDRY, DELIVERYTWIN_DATABRICKS_ENABLED="0", DELIVERYTWIN_FOUNDRY_ENABLED="1"):
            self.assertEqual(ai_mode(), "foundry")
            self.assertEqual(foundry_chat_settings()[1], "gpt-4o-mini")

    def test_databricks_can_serve_the_model_while_data_stays_local(self) -> None:
        settings = {key: value for key, value in DATABRICKS.items() if key != "DATABRICKS_HTTP_PATH"}
        with environment(**settings, DELIVERYTWIN_DATABRICKS_ENABLED="1"):
            self.assertEqual(resolve_dialect(), "duckdb")
            self.assertEqual(ai_mode(), "databricks_serving")

    def test_the_switch_beats_the_dialect_setting_but_not_the_command_line(self) -> None:
        with environment(**DATABRICKS, DELIVERYTWIN_DATABRICKS_ENABLED="0", DELIVERYTWIN_DB_DIALECT="databricks"):
            self.assertEqual(resolve_dialect(), "duckdb")
            self.assertEqual(resolve_dialect("databricks"), "databricks")

    def test_unset_switches_keep_the_automatic_behaviour(self) -> None:
        with environment(**DATABRICKS, **FOUNDRY):
            self.assertEqual(resolve_dialect(), "databricks")
            self.assertEqual(ai_mode(), "databricks_serving")
            self.assertEqual(describe(), {"databricks": "auto", "foundry": "auto"})
        with environment():
            self.assertEqual(resolve_dialect(), "duckdb")

class FoundryOnlyInTheCloudTests(unittest.TestCase):
    def test_the_model_endpoint_is_derived_from_the_project_unless_set(self) -> None:
        with environment(**FOUNDRY):
            self.assertEqual(foundry_settings()["endpoint"], "https://foundry-res.services.ai.azure.com")
            self.assertEqual(foundry_settings()["api_version"], "2024-05-01-preview")
        with environment(**FOUNDRY, FOUNDRY_ENDPOINT="https://custom.cognitiveservices.azure.com/"):
            self.assertEqual(foundry_settings()["endpoint"], "https://custom.cognitiveservices.azure.com")
        with environment(FOUNDRY_PROJECT_ENDPOINT="https://not-a-project.example.com", FOUNDRY_MODEL="m"):
            self.assertIsNone(foundry_settings())

    def test_app_service_ignores_the_openai_key_and_model_serving(self) -> None:
        with environment(**DATABRICKS, OPENAI_API_KEY="sk-local", WEBSITE_SITE_NAME="deliverytwin-demo"):
            self.assertEqual(ai_mode(), "off")
        with environment(**DATABRICKS, **FOUNDRY, OPENAI_API_KEY="sk-local", WEBSITE_SITE_NAME="deliverytwin-demo"):
            self.assertEqual(ai_mode(), "foundry")
        with environment(**FOUNDRY, OPENAI_API_KEY="sk-local", WEBSITE_SITE_NAME="deliverytwin-demo",
                         DELIVERYTWIN_FOUNDRY_ENABLED="0"):
            self.assertEqual(ai_mode(), "off")  # Foundry switched off leaves no model in the cloud

    def test_locally_the_openai_key_still_works_for_testing(self) -> None:
        with environment(OPENAI_API_KEY="sk-local"):
            self.assertEqual(ai_mode(), "openai")
        with environment(**FOUNDRY, OPENAI_API_KEY="sk-local"):
            self.assertEqual(ai_mode(), "foundry")


class EnvFileTests(unittest.TestCase):
    def test_comments_and_quotes(self) -> None:
        text = (
            "# heading\n"
            "BLANK_WITH_COMMENT=              # https://example\n"
            "ONLY_COMMENT=# nothing here\n"
            "PLAIN=value  # trailing note\n"
            "HASH_INSIDE=abc#def\n"
            "QUOTED=\"keep # this\"   # but not this\n"
            "SINGLE='x'\n"
        )
        names = ("BLANK_WITH_COMMENT", "ONLY_COMMENT", "PLAIN", "HASH_INSIDE", "QUOTED", "SINGLE")
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in names}, clear=True):
            path = Path(folder) / ".env"
            path.write_text(text, encoding="utf-8")
            load_env_file(path)
            self.assertEqual([os.environ[name] for name in names], ["", "", "value", "abc#def", "keep # this", "x"])


if __name__ == "__main__":
    unittest.main()
