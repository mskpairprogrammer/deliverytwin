"""End-to-end journey test: SHAPE -> COMMIT -> TRACK -> INTERVENE in a real browser.

Skipped when Playwright or its Chromium build is not installed
(``pip install playwright`` then ``playwright install chromium``).
"""
import http.client
import json
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

try:
    from tests.llm_stub import ScriptedLLM
except ImportError:  # discovery with -s tests
    from llm_stub import ScriptedLLM


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipIf(sync_playwright is None, "playwright is not installed")
class JourneyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.port = _free_port()
        cls.llm_enabled = False
        cls.llm = ScriptedLLM()
        # The server runs in this process, so patching the module covers its request threads.
        cls.patches = [
            patch("deliverytwin.intake.openai_configured", side_effect=lambda: cls.llm_enabled),
            patch("deliverytwin.intake.complete", side_effect=lambda *a, **k: cls.llm(*a, **k)),
        ]
        for active in cls.patches:
            active.start()
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
        for active in cls.patches:
            active.stop()
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        type(self).llm_enabled = False
        type(self).llm = ScriptedLLM()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", "/api/reset-all", json.dumps({"confirm": True}),
                           {"Content-Type": "application/json"})
        self.assertEqual(connection.getresponse().status, 200)
        connection.close()
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

    def run_shape_to_estimate(self) -> None:
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertEqual(self.states()["shape"], "Estimate ready")

    def run_shape_to_commit(self) -> None:
        page = self.page
        self.run_shape_to_estimate()
        analog_rows = page.locator("#rom-analogs tr")
        self.assertEqual(analog_rows.count(), 4)
        self.assertRegex(analog_rows.nth(1).text_content(), r"\d+%.*\d+ h.*\d+ h")
        page.get_by_role("button", name="Continue to COMMIT →").click()
        page.locator("#commit-role-chart svg").wait_for()

    def test_shape_is_three_steps_that_collapse_and_survive_a_reload(self) -> None:
        page = self.page
        visible = lambda: [n for n in (1, 2, 3) if page.locator(f"#intake-step-{n}").is_visible()]
        collapsed = lambda: [n for n in (1, 2, 3) if "collapsed" in (page.locator(f"#intake-step-{n}").get_attribute("class") or "")]
        self.assertEqual(visible(), [1])
        self.assertEqual(page.locator("#intake-step-badge").inner_text(), "STEP 1 OF 3")

        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").wait_for()
        self.assertEqual((visible(), collapsed()), ([1, 2], [1]))
        self.assertIn("We need one view of every customer", page.locator("#summary-1").inner_text())
        cards = page.locator("#intake-questions .q-card")
        self.assertEqual(cards.count(), 4)
        self.assertEqual(page.locator("#intake-questions .q-support").count(), 4)
        self.assertRegex(page.locator("#intake-questions .q-support").first.inner_text(), r"Suggested by \d+ of the \d+ closest projects")
        self.assertEqual(page.locator("#intake-questions input:checked").count(), 4)
        self.assertEqual(page.locator("#intake-profile").count(), 0)

        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertEqual((visible(), collapsed()), ([1, 2, 3], [1, 2]))
        self.assertEqual(page.locator("#intake-step-badge").inner_text(), "STEP 3 OF 3")
        self.assertIn("Architecture:", page.locator("#summary-2").inner_text())
        hero = page.locator("#intake-rom").inner_text()
        for label in ("Most likely effort (P50)", "Commit-to effort (P80)", "Worst case (P90)", "On-time probability"):
            self.assertIn(label, hero)
        self.assertFalse(page.locator("#intake-evidence").evaluate("node => node.open"))
        self.assertTrue(page.get_by_role("button", name="Continue to COMMIT →").is_visible())

        # A standard-question analysis is not restored, so a working AI service is not hidden.
        page.reload()
        page.wait_for_function("document.getElementById('intake-text').value.length > 0")
        self.assertEqual(visible(), [1])
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertEqual(self.states()["shape"], "Estimate ready")

        page.locator('[data-edit="2"]').click()
        self.assertEqual((visible(), collapsed()), ([1, 2], [1]))
        self.assertEqual(self.states()["shape"], "Clarifying request")
        self.assertEqual(self.states()["commit"], "Needs an estimate")
        page.locator('#intake-questions input[value="batch_sync"]').check()
        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertIn("batch", page.locator("#intake-fingerprint").text_content().lower())

        page.locator('[data-edit="1"]').click()
        self.assertEqual((visible(), collapsed()), ([1], []))

    def test_full_journey_through_every_stage(self) -> None:
        page = self.page
        self.go("commit")
        self.assertTrue(page.locator("#commit-empty").is_visible())
        self.assertEqual(self.states()["commit"], "Needs an estimate")
        page.get_by_role("button", name="Go to SHAPE").click()
        self.assertEqual(page.locator("#tab-shape").get_attribute("aria-selected"), "true")

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

    def test_llm_writes_the_questions_and_its_forecast_is_the_central_result(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").wait_for()
        self.assertIn("Tailored questions written by", page.locator("#intake-retrieval").inner_text())
        self.assertEqual(page.locator("#intake-questions .q-card").count(), 4)
        self.assertEqual([text.strip() for text in page.locator("#intake-questions legend").all_text_contents()],
                         ["Delivery", "Data", "Privacy", "Reuse"])
        self.assertIn("Most similar projects phased the move", page.locator("#intake-questions .q-support").first.inner_text())
        self.assertEqual(page.locator("#intake-questions input:checked").count(), 4)

        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertRegex(page.locator("#prediction-source").inner_text(), r"AI FORECAST · .+\n?.*Model confidence 77%")
        hero = page.locator("#intake-rom").inner_text()
        for value in ("4,900 h", "6,100 h", "6,900 h", "64%"):
            self.assertIn(value, hero)
        self.assertIn("Identity matching rules are shared", page.locator("#prediction-risks").inner_text())
        self.assertIn("Phase scope", page.locator("#prediction-recs").inner_text())
        self.assertGreaterEqual(page.locator("#prediction-projects .project-card").count(), 1)
        self.assertIn("Deterministic ROM for comparison", page.locator("#prediction-compare").inner_text())
        self.assertTrue(page.get_by_role("button", name="Continue to COMMIT →").is_visible())

        page.get_by_role("button", name="Continue to COMMIT →").click()
        page.locator("#commit-role-chart svg").wait_for()
        self.assertIn("AI forecast baseline", page.locator("#intake-commit-explain").inner_text())
        page.locator("#tune-scope").fill("20")
        page.get_by_role("button", name="Recalculate confidence").click()
        page.wait_for_function("document.getElementById('intake-commit-explain').textContent.includes('20% scope change')")
        page.get_by_role("button", name="Reset to defaults").click()
        page.wait_for_function("!document.getElementById('intake-commit-explain').textContent.includes('scope change')")
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        self.assertIn("P50 4,900 h · P80 6,100 h", page.locator("#intake-approved").inner_text())
        self.assertEqual(type(self).llm.calls.count("questions"), 1)
        self.assertEqual(type(self).llm.calls.count("forecast"), 1)

    def test_an_ai_estimate_survives_a_reload_without_calling_the_model_again(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        page.reload()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertIn("4,900 h", page.locator("#intake-rom").inner_text())
        self.assertIn("AI FORECAST", page.locator("#prediction-source").inner_text())
        self.assertEqual(self.states()["shape"], "Estimate ready")
        self.assertEqual(type(self).llm.calls.count("questions"), 1)
        self.assertEqual(type(self).llm.calls.count("forecast"), 1)

    def test_an_unusable_ai_forecast_is_labeled_and_the_flow_still_works(self) -> None:
        type(self).llm_enabled = True
        type(self).llm = ScriptedLLM(forecast="invalid")
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        self.assertIn("STATISTICAL ESTIMATE", page.locator("#prediction-source").inner_text())
        page.get_by_role("button", name="Continue to COMMIT →").click()
        page.locator("#commit-role-chart svg").wait_for()
        self.assertIn("Statistical estimate baseline", page.locator("#intake-commit-explain").inner_text())
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")

    def test_a_failing_ai_service_is_reported_honestly_with_a_retry(self) -> None:
        type(self).llm_enabled = True
        type(self).llm = ScriptedLLM(fail=("questions", "forecast"))
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").wait_for()
        notice = page.locator("#intake-retrieval").inner_text()
        self.assertIn("could not be reached", notice)
        self.assertIn("HTTP 401", notice)
        self.assertNotIn("No AI model is configured", notice)
        self.assertEqual(page.locator("#intake-retrieval button").inner_text(), "Try again with AI")

        type(self).llm = ScriptedLLM()
        page.locator("#intake-retrieval button").click()
        page.wait_for_function("document.getElementById('intake-retrieval').textContent.includes('Tailored questions written by')")
        self.assertEqual(page.locator("#intake-questions .q-card").count(), 4)

        type(self).llm = ScriptedLLM(fail=("forecast",))
        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        pill = page.locator("#prediction-source").inner_text()
        self.assertIn("STATISTICAL ESTIMATE", pill)
        self.assertIn("HTTP 429", pill)
        type(self).llm = ScriptedLLM()
        page.locator("#prediction-source button").click()
        page.wait_for_function("document.getElementById('prediction-source').textContent.includes('AI FORECAST ·')")
        self.assertIn("4,900 h", page.locator("#intake-rom").inner_text())

    def test_documents_can_be_attached_before_analyzing_and_reach_the_model(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        # Choosing the sample must not advance the flow; the attach controls stay available.
        self.assertTrue(page.locator("#intake-step-1").is_visible())
        self.assertTrue(page.locator("#intake-step-2").is_hidden())
        directory = tempfile.mkdtemp()
        rom, notes = os.path.join(directory, "rom.txt"), os.path.join(directory, "notes.md")
        with open(rom, "w", encoding="utf-8") as handle:
            handle.write("ROM: Customer 360 with identity resolution and consent review, about 5000 hours.")
        with open(notes, "w", encoding="utf-8") as handle:
            handle.write("Meeting notes: privacy office must sign off before profiles are shared.")
        page.locator("#intake-rom-file").set_input_files(rom)
        page.locator("#intake-notes-file").set_input_files(notes)
        page.wait_for_function("document.querySelectorAll('#intake-doc-list li').length === 2")
        self.assertIn("ROM: rom.txt", page.locator("#intake-doc-list").inner_text())
        self.assertIn("Meeting notes: notes.md", page.locator("#intake-doc-list").inner_text())
        self.assertTrue(page.locator("#intake-step-2").is_hidden())

        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").wait_for()
        self.assertIn("2 document(s)", page.locator("#summary-1").inner_text())
        sent = " ".join(type(self).llm.prompts)
        self.assertIn("privacy office must sign off", sent)

        # Documents can still be added later: Edit reopens step 1 with the attachments still listed.
        page.locator('[data-edit="1"]').click()
        self.assertTrue(page.locator("#intake-step-1").is_visible())
        self.assertEqual(page.locator("#intake-doc-list li").count(), 2)

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
