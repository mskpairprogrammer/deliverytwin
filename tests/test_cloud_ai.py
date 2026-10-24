"""AI routing: Databricks Model Serving, then Microsoft Foundry, then OpenAI, with truthful failures."""
import io
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from deliverytwin.ai_common import llm_json
from deliverytwin.llm import LLMError, ai_status, complete, databricks_serving_settings, openai_configured, openai_model

NONE = {
    "DELIVERYTWIN_AI_MODE": "", "OPENAI_API_KEY": "", "FOUNDRY_ENDPOINT": "", "FOUNDRY_MODEL": "",
    "FOUNDRY_API_KEY": "", "DATABRICKS_SERVING_ENDPOINT": "", "COPILOT_AI_ENDPOINT": "", "DATABRICKS_HOST": "",
    "DATABRICKS_TOKEN": "", "DATABRICKS_CLIENT_ID": "", "DATABRICKS_CLIENT_SECRET": "",
}
DATABRICKS = {"DATABRICKS_SERVING_ENDPOINT": "chat-endpoint", "DATABRICKS_HOST": "https://adb-123.azuredatabricks.net/",
              "DATABRICKS_TOKEN": "dapi-secret"}
AZURE = {"FOUNDRY_ENDPOINT": "https://example.openai.azure.com", "FOUNDRY_MODEL": "dep",
         "FOUNDRY_API_KEY": "azure-key"}


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def reply(content: str) -> Response:
    return Response(json.dumps({"choices": [{"message": {"content": content}}]}).encode())


def env(**values):
    return patch.dict(os.environ, {**NONE, **values})


class RoutingTests(unittest.TestCase):
    def test_priority_is_databricks_then_azure_then_openai_then_off(self) -> None:
        with env():
            self.assertEqual((ai_status()["mode"], openai_configured()), ("off", False))
        with env(OPENAI_API_KEY="sk-x"):
            self.assertEqual(ai_status()["mode"], "openai")
        with env(OPENAI_API_KEY="sk-x", **AZURE):
            self.assertEqual((ai_status()["mode"], openai_model()), ("foundry", "dep"))
        with env(OPENAI_API_KEY="sk-x", **AZURE, **DATABRICKS):
            self.assertEqual(ai_status(), {"mode": "databricks_serving", "model": "chat-endpoint",
                                           "label": "Databricks Model Serving",
                                           "integrations": {"databricks": "auto", "foundry": "auto"}})

    def test_the_copilot_endpoint_name_is_an_alias_and_recorded_mode_still_wins(self) -> None:
        values = {**DATABRICKS, "DATABRICKS_SERVING_ENDPOINT": "", "COPILOT_AI_ENDPOINT": "copilot-chat"}
        with env(**values):
            self.assertEqual(openai_model(), "copilot-chat")
        with env(**DATABRICKS, DELIVERYTWIN_AI_MODE="recorded"):
            self.assertEqual(ai_status()["mode"], "recorded")

    def test_incomplete_or_unsafe_databricks_settings_are_not_used(self) -> None:
        for override in ({"DATABRICKS_TOKEN": ""}, {"DATABRICKS_HOST": ""}, {"DATABRICKS_SERVING_ENDPOINT": ""},
                         {"DATABRICKS_SERVING_ENDPOINT": "../admin/secrets"}, {"DATABRICKS_HOST": "evil.example/x"}):
            with env(**{**DATABRICKS, **override}):
                self.assertIsNone(databricks_serving_settings(), override)
        with env(**{**DATABRICKS, "DATABRICKS_TOKEN": "", "DATABRICKS_CLIENT_ID": "id", "DATABRICKS_CLIENT_SECRET": "s"}):
            self.assertEqual(databricks_serving_settings()["endpoint"], "chat-endpoint")


class DatabricksCallTests(unittest.TestCase):
    def test_it_posts_to_the_serving_endpoint_with_a_bearer_token(self) -> None:
        seen = {}

        def fake(request, timeout):
            seen.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data))
            return reply("{\"ok\": true}")

        with env(**DATABRICKS), patch("urllib.request.urlopen", fake):
            self.assertEqual(complete([{"role": "user", "content": "hi"}], json_mode=True), "{\"ok\": true}")
        self.assertEqual(seen["url"], "https://adb-123.azuredatabricks.net/serving-endpoints/chat-endpoint/invocations")
        self.assertEqual(seen["headers"]["Authorization"], "Bearer dapi-secret")
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(seen["body"]["temperature"], 0)

    def test_an_endpoint_that_rejects_response_format_is_retried_without_it(self) -> None:
        bodies = []

        def fake(request, timeout):
            body = json.loads(request.data)
            bodies.append(body)
            if "response_format" in body:
                raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, None)
            return reply("{\"ok\": 1}")

        with env(**DATABRICKS), patch("urllib.request.urlopen", fake):
            self.assertEqual(complete([{"role": "user", "content": "hi"}], json_mode=True), "{\"ok\": 1}")
        self.assertEqual(len(bodies), 2)
        self.assertNotIn("response_format", bodies[1])

    def test_failures_are_reported_truthfully_and_become_labeled_fallbacks(self) -> None:
        def failing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 503, "down", {}, None)

        with env(**DATABRICKS), patch("urllib.request.urlopen", failing):
            with self.assertRaises(LLMError) as raised:
                complete([{"role": "user", "content": "hi"}])
            self.assertIn("Databricks Model Serving request failed with HTTP 503", str(raised.exception))
            data, reason = llm_json([{"role": "user", "content": "hi"}])
        self.assertIsNone(data)
        self.assertEqual(reason["code"], "request_failed")
        self.assertIn("HTTP 503", reason["detail"])
        with env():
            self.assertEqual(llm_json([{"role": "user", "content": "hi"}]), (None, {"code": "not_configured"}))

    def test_a_non_json_reply_is_an_invalid_response_not_a_crash(self) -> None:
        with env(**DATABRICKS), patch("urllib.request.urlopen", lambda request, timeout: reply("plain words")):
            data, reason = llm_json([{"role": "user", "content": "hi"}])
        self.assertEqual((data, reason["code"]), (None, "invalid_response"))

    def test_the_azure_call_is_used_when_databricks_is_not_configured(self) -> None:
        seen = {}

        def fake(request, timeout):
            seen["url"] = request.full_url
            return reply("azure")

        with env(**AZURE), patch("urllib.request.urlopen", fake):
            self.assertEqual(complete([{"role": "user", "content": "hi"}]), "azure")
        self.assertIn("/openai/deployments/dep/", seen["url"])

    def test_foundry_models_endpoint_uses_model_inference_route(self) -> None:
        seen = {}

        def fake(request, timeout):
            seen.update(url=request.full_url, body=json.loads(request.data))
            return reply("{\"ok\": true}")

        settings = {
            "FOUNDRY_ENDPOINT": "https://example.services.ai.azure.com",
            "FOUNDRY_MODEL": "dep",
            "FOUNDRY_API_KEY": "azure-key",
        }
        with env(**settings), patch("urllib.request.urlopen", fake):
            self.assertEqual(
                complete([{"role": "user", "content": "hi"}], json_mode=True),
                "{\"ok\": true}",
            )
        self.assertEqual(
            seen["url"],
            "https://example.services.ai.azure.com/models/chat/completions?api-version=2024-05-01-preview",
        )
        self.assertEqual(seen["body"]["model"], "dep")
        self.assertNotIn("temperature", seen["body"])
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})


def rejected(request, detail: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": detail, "param": "temperature"}}).encode()
    return urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(body))


TEMPERATURE_REJECTED = "Unsupported value: 'temperature' does not support 0 with this model."


class ReasoningModelTests(unittest.TestCase):
    def test_a_model_that_rejects_temperature_is_asked_again_without_it(self) -> None:
        bodies = []

        def fake(request, timeout):
            body = json.loads(request.data)
            bodies.append(body)
            if "temperature" in body:
                raise rejected(request, TEMPERATURE_REJECTED)
            return reply("{\"ok\": true}")

        with env(OPENAI_API_KEY="sk-x", OPENAI_MODEL="gpt-6-luna"), patch("urllib.request.urlopen", fake):
            self.assertEqual(complete([{"role": "user", "content": "hi"}], json_mode=True), "{\"ok\": true}")
        self.assertEqual([body.get("temperature") for body in bodies], [0, None])
        self.assertEqual(bodies[1]["response_format"], {"type": "json_object"})
        self.assertEqual(bodies[1]["model"], "gpt-6-luna")

    def test_other_bad_requests_are_not_retried(self) -> None:
        calls = []

        def fake(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b"{\"error\": \"context too long\"}"))

        with env(OPENAI_API_KEY="sk-x"), patch("urllib.request.urlopen", fake):
            with self.assertRaises(LLMError) as raised:
                complete([{"role": "user", "content": "hi"}])
        self.assertEqual(len(calls), 1)
        self.assertIn("HTTP 400", str(raised.exception))

    def test_foundry_deployments_and_databricks_serving_retry_too(self) -> None:
        for settings in (AZURE, DATABRICKS):
            bodies = []

            def fake(request, timeout):
                body = json.loads(request.data)
                bodies.append(body)
                if "temperature" in body:
                    raise rejected(request, TEMPERATURE_REJECTED)
                return reply("done")

            with env(**settings), patch("urllib.request.urlopen", fake):
                self.assertEqual(complete([{"role": "user", "content": "hi"}]), "done", settings)
            self.assertEqual(len(bodies), 2, settings)
            self.assertNotIn("temperature", bodies[1])


if __name__ == "__main__":
    unittest.main()
