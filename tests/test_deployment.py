"""Azure App Service readiness: the code package, host/port detection, and the infrastructure template."""
from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from deliverytwin.app import _managed_host
from scripts.package_app import package

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_the_package_holds_the_runnable_app_and_nothing_local(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            names = package(Path(folder) / "app.zip")
            with zipfile.ZipFile(Path(folder) / "app.zip") as archive:
                self.assertEqual(sorted(archive.namelist()), names)
        for required in ("requirements.txt", "deliverytwin/__main__.py", "deliverytwin/dashboard.html",
                         "deliverytwin/schema.sql", "data/samples/sample_rom_customer_360.md",
                         "data/delivery_intelligence/project_intakes.csv"):
            self.assertIn(required, names)
        self.assertFalse([name for name in names if "\\" in name or "__pycache__" in name
                          or name.endswith((".duckdb", ".db", ".wal", ".pyc")) or Path(name).name.startswith(".env")])
        self.assertFalse([name for name in names if name.startswith(("tests/", "scripts/"))])

    def test_app_service_and_databricks_apps_bind_beyond_loopback(self) -> None:
        clean = {key: value for key, value in os.environ.items() if key not in ("WEBSITE_SITE_NAME", "DATABRICKS_APP_PORT")}
        with patch.dict(os.environ, clean, clear=True):
            self.assertFalse(_managed_host())
            with patch.dict(os.environ, {"WEBSITE_SITE_NAME": "deliverytwin-demo"}):
                self.assertTrue(_managed_host())
            with patch.dict(os.environ, {"DATABRICKS_APP_PORT": "8000"}):
                self.assertTrue(_managed_host())

    def test_the_template_never_trusts_a_gateway_it_does_not_configure(self) -> None:
        template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("{ name: 'DELIVERYTWIN_TRUSTED_AUTH', value: '1' }", template)
        self.assertIn("name: 'authsettingsV2'", template)
        self.assertIn("requireAuthentication: true", template)
        self.assertIn("appCommandLine: 'python -m deliverytwin serve'", template)
        self.assertIn("healthCheckPath: '/healthz'", template)
        self.assertIn("httpsOnly: true", template)

    def test_the_template_ships_foundry_and_databricks_switched_off(self) -> None:
        template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        self.assertIn("param enableFoundry bool = false", template)
        self.assertIn("param enableDatabricks bool = false", template)
        self.assertIn("{ name: 'DELIVERYTWIN_FOUNDRY_ENABLED', value: enableFoundry ? '1' : '0' }", template)
        self.assertIn("{ name: 'DELIVERYTWIN_DATABRICKS_ENABLED', value: enableDatabricks ? '1' : '0' }", template)
        self.assertIn("@secure()\nparam databricksToken string = ''", template.replace("\r\n", "\n"))

    def test_a_private_package_index_is_a_secret_optional_setting(self) -> None:
        template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertIn("@secure()\nparam pipIndexUrl string = ''", template)
        self.assertIn("{ name: 'PIP_INDEX_URL', value: pipIndexUrl }", template)

    def test_the_cloud_gets_foundry_models_only(self) -> None:
        template = (ROOT / "infra" / "main.bicep").read_text(encoding="utf-8")
        for absent in ("name: 'OPENAI_API_KEY'", "AZURE_OPENAI", "DATABRICKS_SERVING_ENDPOINT", "FOUNDRY_API_KEY"):
            self.assertNotIn(absent, template)
        for present in ("FOUNDRY_PROJECT_ENDPOINT", "FOUNDRY_MODEL", "FOUNDRY_ENDPOINT"):
            self.assertIn(f"name: '{present}'", template)

    def test_databricks_libraries_are_installed_only_when_asked(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            for with_databricks in (False, True):
                path = Path(folder) / f"app-{with_databricks}.zip"
                package(path, with_databricks)
                with zipfile.ZipFile(path) as archive:
                    text = archive.read("requirements.txt").decode("utf-8")
                self.assertIn("duckdb", text)
                self.assertEqual("databricks-sql-connector" in text, with_databricks)
                self.assertNotIn("-r ", text)


if __name__ == "__main__":
    unittest.main()
