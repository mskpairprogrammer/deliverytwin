"""End-to-end journey test: SHAPE -> COMMIT -> TRACK -> INTERVENE in a real browser.

Skipped when Playwright or its Chromium build is not installed
(``pip install playwright`` then ``playwright install chromium``).
"""
import os
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - optional dependency
    sync_playwright = None

from deliverytwin.app import serve_dashboard


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class JourneyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = _free_port()
        database = os.path.join(tempfile.mkdtemp(), "ui.db")
        with patch.dict(os.environ, {"DELIVERYTWIN_DEMO_MODE": "1"}):
            threading.Thread(
                target=serve_dashboard, args=(database, "127.0.0.1", cls.port), daemon=True
            ).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch()
        except Exception as error:  # Chromium not downloaded
            cls.playwright.stop()
            raise unittest.SkipTest(f"Chromium is unavailable: {error}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.page = self.browser.new_page(viewport={"width": 1280, "height": 900})
        self.errors: list[str] = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.set_default_timeout(15000)
        self.page.goto(f"http://127.0.0.1:{self.port}/#shape")

    def tearDown(self) -> None:
        self.page.close()
        self.assertEqual(self.errors, [], "the page raised JavaScript errors")

    def states(self) -> dict[str, str]:
        return {
            stage: self.page.locator(f"#state-{stage}").inner_text()
            for stage in ("shape", "commit", "track", "intervene")
        }

    def go(self, stage: str) -> None:
        self.page.evaluate(f"document.getElementById('tab-{stage}').click()")

    def run_shape_to_commit(self) -> None:
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Confirm answers & match fingerprint").click()
        page.get_by_role("button", name="Generate explainable ROM").click()
        page.locator("#intake-rom").get_by_text("P50 effort").wait_for()
        self.assertEqual(self.states()["shape"], "Estimate ready")
        analog_rows = page.locator("#rom-analogs tr").all_inner_texts()
        self.assertEqual(len(analog_rows), 4, analog_rows)
        self.assertRegex(analog_rows[1], r"\d+%.*\d+ h.*\d+ h")
        page.get_by_role("button", name="Continue to COMMIT →").click()
        page.locator("#commit-role-chart svg").wait_for()

    def test_full_journey_through_every_stage(self) -> None:
        page = self.page
        self.go("commit")
        self.assertTrue(page.locator("#commit-empty").is_visible())
        self.assertEqual(self.states()["commit"], "Needs an estimate")
        page.get_by_role("button", name="Go to SHAPE").click()
        self.assertEqual(page.locator("#tab-shape").get_attribute("aria-selected"), "true")
        self.assertTrue(page.locator("#shape-summary").is_hidden())

        self.run_shape_to_commit()
        self.assertEqual(page.locator("#tab-commit").get_attribute("aria-selected"), "true")
        self.assertTrue(page.locator("#commit-empty").is_hidden())
        self.assertEqual(self.states()["commit"], "Awaiting approval")

        roles = page.locator("#commit-role-chart").inner_text()
        for role in ("Customer 360 Lead", "Customer Data Engineer", "CRM Integration Engineer",
                     "Customer Privacy Engineer"):
            self.assertIn(role, roles)
        self.assertIn("Feasible", page.locator("#intake-commit-explain").inner_text())
        starting = page.locator(".scenario-table").inner_text()

        page.locator("#tune-fte").fill("3")
        page.get_by_role("button", name="Recalculate confidence").click()
        page.wait_for_function("document.getElementById('intake-approve').disabled")
        self.assertIn("Not feasible", page.locator(".scenario-table").inner_text())
        self.assertIn("add capacity", page.locator("#intake-approve").get_attribute("title"))

        page.get_by_role("button", name="Reset to defaults").click()
        page.wait_for_function("!document.getElementById('intake-approve').disabled")
        self.assertEqual(page.locator(".scenario-table").inner_text(), starting)

        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        self.assertEqual(self.states()["commit"], "Baseline approved")
        page.get_by_role("button", name="Continue to TRACK →").click()
        self.assertEqual(page.locator("#tab-track").get_attribute("aria-selected"), "true")
        self.assertEqual(self.states()["track"], "Not started")
        self.assertTrue(page.locator("#track-next").is_hidden())

        page.get_by_role("button", name="Simulate next sprint").click()
        page.locator("#track-next").wait_for(state="visible")
        self.assertRegex(self.states()["track"], r"^Sprint 1 · \d+% on time$")
        titles = page.locator("#panel-track .card-title").all_inner_texts()
        self.assertTrue(titles[0].startswith("Sprint 1 delivery outlook"), titles)

        page.get_by_role("button", name="Review recovery options →").click()
        self.assertEqual(page.locator("#tab-intervene").get_attribute("aria-selected"), "true")
        self.assertRegex(self.states()["intervene"], r"^Option [ABC] recommended$")

        page.reload()
        page.wait_for_function("document.getElementById('state-commit').textContent === 'Baseline approved'")
        self.assertEqual(self.states()["shape"], "Estimate ready")

    def test_demo_data_can_be_cleared_from_the_footer(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        page.evaluate("window.confirm = () => true")
        page.get_by_role("button", name="Clear all demo data").click()
        page.wait_for_function("document.getElementById('clear-data-status').textContent.startsWith('Cleared')")
        self.assertEqual(
            self.states(),
            {"shape": "Start here", "commit": "Needs an estimate",
             "track": "Not started", "intervene": "Waiting for signals"},
        )
        self.go("commit")
        self.assertTrue(page.locator("#commit-empty").is_visible())


if __name__ == "__main__":
    unittest.main()
