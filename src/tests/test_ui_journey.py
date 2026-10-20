"""End-to-end journey test: SHAPE -> COMMIT -> TRACK -> INTERVENE in a real browser.

Skipped when Playwright or its Chromium build is not installed
(``pip install playwright`` then ``playwright install chromium``).
"""
import http.client
import json
import os
import re
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
    from tests.llm_stub import ScriptedLLM, llm_questions
except ImportError:  # discovery with -s tests
    from llm_stub import ScriptedLLM, llm_questions


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
            patch("deliverytwin.recovery.openai_configured", side_effect=lambda: cls.llm_enabled),
            patch("deliverytwin.recovery.complete", side_effect=lambda *a, **k: cls.llm(*a, **k)),
            patch("deliverytwin.ai_common.openai_configured", side_effect=lambda: cls.llm_enabled),
            patch("deliverytwin.ai_common.complete", side_effect=lambda *a, **k: cls.llm(*a, **k)),
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
        page.wait_for_function("document.getElementById('review-risks').children.length > 0")
        self.assertIn("RULES-BASED REVIEW", page.locator("#review-source").inner_text())
        starting = page.locator(".scenario-table").inner_text()

        page.locator("#tune-fte").fill("3")
        page.get_by_role("button", name="Recalculate confidence").click()
        page.wait_for_function("document.getElementById('intake-approve').disabled")
        self.assertIn("Not feasible", page.locator(".scenario-table").inner_text())
        self.assertIn("add capacity", page.locator("#intake-approve").get_attribute("title"))
        page.wait_for_function("document.getElementById('review-source').textContent.includes('NOT READY')")

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
        self.assertEqual(titles[0], "AI sprint briefing", titles)
        self.assertTrue(any(title.startswith("Sprint 1 delivery outlook") for title in titles), titles)
        page.wait_for_function("document.getElementById('brief-drivers').children.length > 0")
        self.assertIn("RULES-BASED BRIEFING", page.locator("#brief-source").inner_text())
        self.assertIn("Production support load", page.locator("#brief-drivers").inner_text())

        page.get_by_role("button", name="Review recovery options →").click()
        self.assertEqual(page.locator("#tab-intervene").get_attribute("aria-selected"), "true")
        page.wait_for_function("document.querySelectorAll('#act-options .rec-card').length === 3")
        self.assertIn("RULES-BASED OPTIONS", page.locator("#act-source").inner_text())
        self.assertRegex(self.states()["intervene"], r"^.+ recommended$")

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

    def test_shape_badges_commit_role_phase_guardrail_and_roi_strips(self) -> None:
        type(self).llm_enabled = True
        questions = llm_questions()
        questions["findings"] = [
            {"kind": "risk", "text": "Privacy sign-off gates sharing.", "source": "document",
             "source_document": "notes.md", "source_quote": "privacy office must sign off before profiles"},
            {"kind": "risk", "text": "Vendor lead time is long.", "source": "document",
             "source_document": "notes.md", "source_quote": "the vendor needs ninety days to deliver"},
            {"kind": "fact", "text": "Leadership wants one customer view.", "source": "request",
             "source_quote": "one view of every customer"},
            {"kind": "dependency", "text": "CRM changes may be needed."},
        ]
        type(self).llm = ScriptedLLM(questions=questions)
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        directory = tempfile.mkdtemp()
        notes = os.path.join(directory, "notes.md")
        with open(notes, "w", encoding="utf-8") as handle:
            handle.write("Meeting notes: privacy office must sign off before profiles are shared.")
        page.locator("#intake-notes-file").set_input_files(notes)
        page.wait_for_function("document.querySelectorAll('#intake-doc-list li').length === 1")
        page.get_by_role("button", name="Analyze request").click()
        page.get_by_role("button", name="Run prediction").wait_for()

        findings = page.locator("#intake-findings")
        self.assertTrue(findings.is_visible())
        text = findings.inner_text()
        self.assertIn("Found in Meeting Notes (Quote Verified)", text)
        self.assertIn("Stated in Request", text)
        self.assertIn("AI Inference (Confirm)", text)
        self.assertIn("Downgraded", text)  # the fabricated vendor quote is not in the notes
        self.assertEqual(findings.locator(".src-badge.document").count(), 2)  # the verified one plus its summary chip
        self.assertEqual(findings.locator("li .src-badge.inference").count(), 2)

        page.get_by_role("button", name="Run prediction").click()
        page.locator("#intake-rom").get_by_text("Most likely effort (P50)").wait_for()
        page.get_by_role("button", name="Continue to COMMIT →").click()
        page.locator("#role-phase-table table").wait_for()
        guardrail = page.locator("#privacy-guardrail").inner_text()
        self.assertIn("Privacy Guardrail Active: Oracle HCM & UKG capacity aggregated at role level only", guardrail)
        self.assertIn("minimum N >= 3 anonymity threshold; worker-level PII isolated", guardrail)
        self.assertEqual(page.locator("#role-phase-table tbody tr").count(), 4)
        self.assertEqual(page.locator("#role-phase-table thead th").all_inner_texts(),
                         ["Role", "Discovery", "Build", "Test", "Cutover"])
        self.assertGreaterEqual(page.locator("#role-phase-table td[data-status='unstaffed']").count(), 1)

        page.wait_for_function("!document.getElementById('commit-roi').hidden")
        roi = page.locator("#commit-roi").inner_text()
        self.assertRegex(roi, r"\d+% caught at intake")
        self.assertRegex(roi, r"[−+]\d+% spread")
        self.assertRegex(roi, r"\d+ flagged · \d+ stated")

    def test_p0_preemption_shows_the_swimlane_and_a_portfolio_aware_replan(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        self.approve_and_simulate()
        self.assertTrue(page.locator("#pi-swimlane").is_hidden())
        page.get_by_role("button", name="⚡ Simulate P0 Project Preemption (PI Conflict)").click()
        page.locator("#pi-swimlane").wait_for(state="visible")
        lane = page.locator("#pi-swimlane-table")
        self.assertEqual(lane.locator("thead th").all_inner_texts()[:1], ["Project"])
        text = lane.inner_text()
        for expected in ("P0-REG-2026 Urgent Regulatory Compliance", "PRJ-019 Billing Migration", "PRJ-024 Claims Portal"):
            self.assertIn(expected, text)
        self.assertEqual(lane.locator("td[data-status='collision']").count(), 4)  # current + P0 in Sprints 3 and 4
        self.assertEqual(lane.locator("td[data-status='watch']").count(), 4)      # both siblings in Sprints 3 and 4
        self.assertIn("4.5 of 7.0 FTE", text)

        page.get_by_role("button", name="Review recovery options →").click()
        page.wait_for_function("document.querySelectorAll('#act-options .rec-card').length === 4")
        self.assertIn("PORTFOLIO RE-PLAN", page.locator("#act-source").inner_text())
        self.assertIn("RECOMMENDED: COMPOSED RE-PLAN", page.locator("#act-recommendation").inner_text())
        cards = page.locator("#act-options .rec-card")
        self.assertIn("+$60,000", cards.filter(has_text="Backfill preempted FTE").inner_text())
        self.assertIn("$0", cards.filter(has_text="Re-sequence sprint backlog").inner_text())
        borrow = cards.filter(has_text="Cross-portfolio reallocation").inner_text()
        self.assertIn("PRJ-024", borrow)
        self.assertIn("+2 weeks", borrow.replace("about +2", "+2"))

        page.locator("#pi-replan").wait_for(state="visible")
        self.assertEqual(page.locator("#pi-replan-table thead th").all_inner_texts()[-1], "Next PI")
        self.assertEqual(page.locator("#pi-replan-table thead th.hit").count(), 2)
        self.assertIn("Composed re-plan", page.locator("#pi-replan-summary").inner_text())
        page.locator("#pi-replan-select").select_option("preempted")
        self.assertIn("No action · preempted", page.locator("#pi-replan-summary").inner_text())
        self.assertGreaterEqual(page.locator("#pi-replan-table tr[data-status='shifted'], #pi-replan-table tr[data-status='partly_next_pi']").count(), 1)

        cards.filter(has_text="Backfill preempted FTE").get_by_role("button", name="Preview this scenario").click()
        page.locator("#act-what-if-results").get_by_text("Predicted on-time").wait_for()
        self.assertIn("PORTFOLIO PLANNING MODEL", page.locator("#act-what-if-results").inner_text())
        self.assertIn("Backfill preempted FTE", page.locator("#pi-replan-summary").inner_text())
        page.get_by_role("button", name="Apply simulated updates").click()
        page.wait_for_function("document.getElementById('act-scenario-status').textContent.includes('saved')")
        page.reload()
        self.go("track")
        page.locator("#pi-swimlane").wait_for(state="visible")

    def test_ai_drives_the_intervention_analysis_and_what_if_predictions(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        self.run_shape_to_commit()
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        page.get_by_role("button", name="Continue to TRACK →").click()
        page.get_by_role("button", name="Simulate next sprint").click()
        page.locator("#track-next").wait_for(state="visible")
        llm = type(self).llm
        calls = lambda name: llm.calls.count(name)

        page.get_by_role("button", name="Review recovery options →").click()
        page.wait_for_function("document.querySelectorAll('#act-options .rec-card').length === 3")
        self.assertIn("AI RECOVERY ANALYSIS", page.locator("#act-source").inner_text())
        self.assertIn("production support", page.locator("#act-diagnosis").inner_text())
        self.assertIn("RECOMMENDED: EXTEND DATE", page.locator("#act-recommendation").inner_text())
        recommended = page.locator("#act-options .rec-card.recommended")
        self.assertEqual(recommended.count(), 1)
        self.assertIn("83%", recommended.inner_text())
        self.assertIn("Estimated cost impact", recommended.inner_text())
        self.assertIn("+$", page.locator("#act-options .rec-card").last.inner_text())
        self.assertEqual(self.states()["intervene"], "Extend Date recommended")
        self.assertEqual(calls("recovery_analysis"), 1)

        # Previewing an AI scenario reuses its prediction instead of asking again.
        page.locator("#act-options .rec-card").last.get_by_role("button", name="Preview this scenario").click()
        page.locator("#act-what-if-results").get_by_text("Predicted on-time").wait_for()
        self.assertIn("71%", page.locator("#act-what-if-results .rom-hero").inner_text())
        self.assertIn("AI PREDICTION", page.locator("#act-what-if-results").inner_text())
        self.assertIn("Escalate the identity-matching CAB blocker", page.locator("#act-sync-servicenow").inner_text())
        self.assertEqual(calls("recovery_prediction"), 0)

        # Editing a lever invalidates the preview; Predict with AI asks the model.
        page.locator("#act-fte").fill("7.5")
        self.assertIn("Inputs changed", page.locator("#act-scenario-status").inner_text())
        self.assertTrue(page.locator("#act-apply").is_disabled())
        page.get_by_role("button", name="Predict with AI").click()
        page.locator("#act-what-if-results").get_by_text("Predicted on-time").wait_for()
        self.assertIn("66%", page.locator("#act-what-if-results .rom-hero").inner_text())
        self.assertIn("Extra capacity helps", page.locator("#act-what-if-results").inner_text())
        self.assertIn("Capacity-model cross-check", page.locator("#act-what-if-results").inner_text())
        self.assertEqual(calls("recovery_prediction"), 1)

        page.get_by_role("button", name="Apply simulated updates").click()
        page.wait_for_function("document.getElementById('act-scenario-status').textContent.includes('saved')")
        self.assertEqual(calls("recovery_prediction"), 1)
        page.reload()
        page.wait_for_function("document.getElementById('act-scenario-status').textContent.includes('saved')")
        self.assertIn("saved", page.locator("#act-scenario-status").inner_text())

    def test_a_failing_ai_in_intervene_falls_back_and_can_retry(self) -> None:
        type(self).llm_enabled = True
        type(self).llm = ScriptedLLM(fail=("recovery",))
        page = self.page
        self.run_shape_to_commit()
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        page.get_by_role("button", name="Continue to TRACK →").click()
        page.get_by_role("button", name="Simulate next sprint").click()
        page.locator("#track-next").wait_for(state="visible")
        page.get_by_role("button", name="Review recovery options →").click()
        page.wait_for_function("document.querySelectorAll('#act-options .rec-card').length === 3")
        source = page.locator("#act-source").inner_text()
        self.assertIn("RULES-BASED OPTIONS", source)
        self.assertIn("HTTP 503", source)
        page.get_by_role("button", name="Predict with AI").click()
        page.locator("#act-what-if-results").get_by_text("Projected slip", exact=True).wait_for()
        self.assertIn("CAPACITY-MODEL ESTIMATE", page.locator("#act-what-if-results").inner_text())
        type(self).llm = ScriptedLLM()
        page.locator("#act-source button").click()
        page.wait_for_function("document.getElementById('act-source').textContent.includes('AI RECOVERY ANALYSIS')")
        self.assertEqual(page.locator("#act-options .rec-card").count(), 3)

    def approve_and_simulate(self) -> None:
        page = self.page
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        page.get_by_role("button", name="Continue to TRACK →").click()
        page.get_by_role("button", name="Simulate next sprint").click()
        page.locator("#track-next").wait_for(state="visible")

    def test_ai_challenges_the_commitment_and_answers_what_if_questions(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        self.run_shape_to_commit()
        llm = type(self).llm
        page.wait_for_function("document.getElementById('review-risks').children.length === 2")
        self.assertIn("AI REVIEW", page.locator("#review-source").inner_text())
        self.assertIn("READY WITH CONDITIONS", page.locator("#review-source").inner_text())
        self.assertIn("Shared identity matching", page.locator("#review-risks").inner_text())
        self.assertIn("Get a dated CAB slot", page.locator("#review-conditions").inner_text())
        self.assertEqual(llm.calls.count("challenge"), 1)

        summary = page.locator("#intake-commitment").inner_text()
        current_fte = float(page.input_value("#tune-fte"))
        page.locator("#ask-input").fill("What if we lose an engineer?")
        page.get_by_role("button", name="Ask AI").click()
        page.locator("#ask-result table").wait_for()
        self.assertIn("Reads as losing one engineer", page.locator("#ask-result").inner_text())
        cells = page.locator("#ask-result tr", has_text="Allocated FTE").locator("td").all_inner_texts()
        self.assertEqual(cells, ["Allocated FTE", f"{current_fte:.1f}", f"{current_fte - 1:.1f}"])
        confidence = page.locator("#ask-result tr", has_text="On-time confidence").locator("td").all_inner_texts()
        self.assertLess(float(confidence[2].rstrip("%")), float(confidence[1].rstrip("%")))
        self.assertEqual(page.locator("#intake-commitment").inner_text(), summary)
        self.assertEqual(llm.calls.count("ask"), 1)

        page.get_by_role("button", name="Use these values").click()
        page.wait_for_function(f"Number(document.getElementById('tune-fte').value) === {current_fte - 1}")
        self.assertTrue(page.locator("#ask-result").is_hidden())
        self.assertNotEqual(page.locator("#intake-commitment").inner_text(), summary)
        page.wait_for_function("document.getElementById('review-risks').children.length === 2")
        self.assertEqual(llm.calls.count("challenge"), 2)

        page.locator("#ask-input").fill("What is the weather like?")
        page.get_by_role("button", name="Ask AI").click()
        page.locator("#ask-result").get_by_text("cannot be expressed").wait_for()
        self.assertEqual(page.locator("#ask-result table").count(), 0)

        page.get_by_role("button", name="What if scope grows 20%?").click()
        page.locator("#ask-result table").wait_for()
        scope = page.locator("#ask-result tr", has_text="Scope change").locator("td").all_inner_texts()
        self.assertEqual(scope[2], "20%")

    def test_approving_the_baseline_generates_an_ado_backlog_that_is_never_sent(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        self.assertTrue(page.locator("#backlog-panel").is_hidden())
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#backlog-tree .wi-type.epic").wait_for()
        self.assertGreaterEqual(page.locator("#backlog-tree .wi-type.feature").count(), 3)
        epic_effort = page.locator("#backlog-tree summary").first.locator(".wi-meta").inner_text()
        self.assertRegex(epic_effort, r"[\d,]+ h · \d{4}-\d{2}-\d{2} → \d{4}-\d{2}-\d{2}")
        self.assertIn("Nothing is sent to Azure DevOps", page.locator("#backlog-panel").inner_text())

        page.get_by_role("button", name="Simulate export to ADO").click()
        export = page.locator("#backlog-export")
        self.assertIn("NOTHING WAS SENT", export.inner_text())
        self.assertEqual(export.locator("code").count(), 1 + page.locator("#backlog-tree .wi-type.feature").count())

        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.get_by_role("button", name="Copy as ADO CSV").click()
        page.get_by_text("Copied the ADO CSV").wait_for()
        copied = page.evaluate("navigator.clipboard.readText()")
        self.assertTrue(copied.startswith("Work Item Type,Title 1,Title 2"))
        self.assertEqual(copied.count("\r\nFeature,"), page.locator("#backlog-tree .wi-type.feature").count())

        # Editing the commitment invalidates the backlog until the baseline is approved again.
        page.locator("#tune-apply").click()
        page.wait_for_function("document.getElementById('backlog-panel').hidden")

    def test_the_full_sample_loads_the_request_rom_and_meeting_notes(self) -> None:
        page = self.page
        page.get_by_role("button", name="Load full sample (request, ROM, notes)").click()
        page.wait_for_function("document.querySelectorAll('#intake-doc-list li').length === 2")
        documents = page.locator("#intake-doc-list").inner_text()
        self.assertIn("ROM: sample_rom_customer_360.md", documents)
        self.assertIn("Meeting notes: sample_meeting_notes_customer_360.md", documents)
        self.assertTrue(page.input_value("#intake-text").startswith("We need one view of every customer"))
        page.get_by_role("button", name="Analyze request").click()
        page.locator("#intake-questions input").first.wait_for()

    def test_the_simulation_lab_switches_scenarios_injects_events_and_imports_exports(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        self.approve_and_simulate()
        page.locator("#sim-lab summary").click()
        confidence = lambda: int(page.locator("#confidence").inner_text())
        lineage = lambda text: page.wait_for_function(
            f"document.getElementById('track-lineage').textContent.includes({json.dumps(text)})")

        page.select_option("#sim-scenario", "healthy")
        page.get_by_role("button", name="Apply scenario").click()
        lineage("HEALTHY DELIVERY")
        healthy = confidence()
        page.select_option("#sim-scenario", "collision")
        page.get_by_role("button", name="Apply scenario").click()
        lineage("CAPACITY COLLISION")
        collision = confidence()
        self.assertGreater(healthy, collision)

        page.get_by_role("button", name="Engineer leaves").click()
        lineage("ENGINEER LEAVES")
        self.assertLess(confidence(), collision)
        self.assertIn("Engineer leaves (sprint", page.locator("#sim-event-log").inner_text())

        page.get_by_role("button", name="Use sample exports").click()
        lineage("IMPORTED EXPORTS")
        page.locator("#import-report table").wait_for()
        report = page.locator("#import-report").inner_text()
        for kind in ("Azure DevOps work items", "ServiceNow incidents", "ServiceNow change requests",
                     "UKG time entries", "UKG time-off requests"):
            self.assertIn(kind, report)
        self.assertEqual(page.locator("#import-report tr").count(), 6)
        self.assertTrue(page.locator("#sim-events button").first.is_disabled())
        self.assertIn("NO LIVE CONNECTION", page.locator("#track-lineage").inner_text())

    def test_applying_a_recovery_scenario_updates_the_backlog_with_a_diff(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        self.approve_and_simulate()
        page.get_by_role("button", name="Review recovery options →").click()
        page.wait_for_function("document.querySelectorAll('#act-options .rec-card').length === 3")
        page.locator("#act-options .rec-card", has_text=re.compile("phase", re.I)).get_by_role(
            "button", name="Preview this scenario").click()
        page.locator("#act-what-if-results").get_by_text("Predicted on-time").wait_for()
        page.get_by_role("button", name="Apply simulated updates").click()
        page.wait_for_function("document.getElementById('act-scenario-status').textContent.includes('saved')")
        self.go("commit")
        page.locator("#backlog-variants").wait_for(state="visible")
        page.locator("#backlog-diff").wait_for(state="visible")
        self.assertIn("Deferred scope: follow-on release", page.locator("#backlog-diff").inner_text())
        self.assertIn("Deferred scope: follow-on release", page.locator("#backlog-tree").inner_text())
        page.get_by_role("button", name="Approved baseline").click()
        self.assertTrue(page.locator("#backlog-diff").is_hidden())
        self.assertNotIn("Deferred scope", page.locator("#backlog-tree").inner_text())

    def test_an_approved_baseline_and_its_backlog_survive_a_reload(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        self.run_shape_to_commit()
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        page.reload()
        page.locator("#intake-approved").wait_for(state="visible")
        page.locator("#backlog-tree .wi-type.epic").wait_for()
        self.assertTrue(page.locator("#intake-approve").is_hidden())
        self.assertEqual(self.states()["commit"], "Baseline approved")

    def test_an_existing_baseline_is_shown_in_commit_without_an_estimate(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        page.reload()
        self.go("commit")
        page.locator("#commit-existing").wait_for(state="visible")
        self.assertIn("A baseline is already approved", page.locator("#commit-existing").inner_text())
        page.locator("#backlog-tree .wi-type.epic").wait_for()

    def test_baseline_history_shows_each_approval_and_what_changed(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        page.wait_for_function("document.getElementById('baseline-history').hidden === false")
        self.assertIn("1 approved", page.locator("#baseline-history-summary").inner_text())
        page.fill("#tune-fte", "8")
        page.locator("#tune-apply").click()
        page.wait_for_function("!document.getElementById('intake-approve').hidden")
        page.get_by_role("button", name="Approve baseline").click()
        page.wait_for_function("document.getElementById('baseline-history-summary').textContent.includes('2 approved')")
        page.locator("#baseline-history-summary").click()
        self.assertEqual(page.locator("#baseline-history-body tr").count(), 3)
        self.assertRegex(page.locator("#baseline-history-body tr").nth(1).inner_text(), r"[+-]")

    def test_the_calibration_view_reports_coverage_of_the_p50_and_p80_bands(self) -> None:
        page = self.page
        page.get_by_role("button", name="Use sample request").click()
        page.get_by_role("button", name="Analyze request").click()
        page.wait_for_function("document.querySelectorAll('#calibration-metrics > *').length === 4")
        self.assertIn("P80", page.locator("#calibration-note").inner_text())
        self.assertIn("target 80%", page.locator("#calibration-metrics").inner_text())

    def test_commit_ai_falls_back_to_rules_and_can_retry(self) -> None:
        type(self).llm_enabled = True
        type(self).llm = ScriptedLLM(fail=("challenge", "ask"))
        page = self.page
        self.run_shape_to_commit()
        page.wait_for_function("document.getElementById('review-source').textContent.includes('RULES-BASED REVIEW')")
        self.assertIn("HTTP 502", page.locator("#review-source").inner_text())
        self.assertGreater(page.locator("#review-risks .review-risk").count(), 0)
        page.locator("#ask-input").fill("What if we lose an engineer?")
        page.get_by_role("button", name="Ask AI").click()
        page.locator("#ask-result").get_by_text("use the sliders instead").wait_for()
        self.assertIn("AI UNAVAILABLE", page.locator("#ask-result").inner_text())
        type(self).llm = ScriptedLLM()
        page.locator("#review-source button").click()
        page.wait_for_function("document.getElementById('review-source').textContent.includes('AI REVIEW')")
        self.assertEqual(page.locator("#review-risks .review-risk").count(), 2)

    def test_ai_briefs_the_sprint_over_computed_variance(self) -> None:
        type(self).llm_enabled = True
        page = self.page
        self.run_shape_to_commit()
        self.approve_and_simulate()
        llm = type(self).llm
        page.wait_for_function("document.getElementById('brief-drivers').children.length > 0")
        self.assertIn("AI BRIEFING", page.locator("#brief-source").inner_text())
        self.assertIn("cut confidence", page.locator("#brief-headline").inner_text())
        self.assertIn("main reasons the forecast moved", page.locator("#brief-narrative").inner_text())
        self.assertIn("Approved baseline", page.locator("#brief-summary").inner_text())
        rows = page.locator("#brief-drivers .var-row")
        self.assertGreater(rows.count(), 1)
        for index in range(rows.count()):
            row = rows.nth(index)
            self.assertRegex(row.locator(".var-num").inner_text(), r"^−[\d.]+ pts · \+[\d.]+ days$")
            self.assertIn("Evidence from", row.locator(".var-why").inner_text())
        self.assertIn("CAB decision on identity matching", page.locator("#brief-watch").inner_text())
        self.assertRegex(page.locator("#brief-badge").inner_text(), r"^\d+ PTS BELOW BASELINE$")
        self.assertEqual(llm.calls.count("brief"), 1)

        self.go("intervene")
        self.go("track")
        self.assertEqual(llm.calls.count("brief"), 1)
        page.get_by_role("button", name="Re-run briefing").click()
        page.wait_for_function("document.getElementById('brief-rerun').disabled === false")
        self.assertEqual(llm.calls.count("brief"), 2)

        page.reload()
        page.wait_for_function("document.getElementById('brief-drivers').children.length > 0")
        self.assertEqual(llm.calls.count("brief"), 2)

    def test_track_briefing_falls_back_to_computed_variance_and_can_retry(self) -> None:
        type(self).llm_enabled = True
        type(self).llm = ScriptedLLM(fail=("brief",))
        page = self.page
        self.run_shape_to_commit()
        self.approve_and_simulate()
        page.wait_for_function("document.getElementById('brief-drivers').children.length > 0")
        source = page.locator("#brief-source").inner_text()
        self.assertIn("RULES-BASED BRIEFING", source)
        self.assertIn("HTTP 500", source)
        self.assertIn("costs about", page.locator("#brief-drivers .var-why").first.inner_text())
        type(self).llm = ScriptedLLM()
        page.locator("#brief-source button").click()
        page.wait_for_function("document.getElementById('brief-source').textContent.includes('AI BRIEFING')")

    def post_status(self, path: str, body: dict | None = None) -> int:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        connection.request("POST", path, json.dumps(body or {}), {"Content-Type": "application/json"})
        status = connection.getresponse().status
        connection.close()
        return status

    def test_track_and_intervene_stay_locked_until_a_baseline_is_approved(self) -> None:
        page = self.page
        for stage in ("track", "intervene"):
            self.assertEqual(self.states()[stage], "Needs approved baseline")
        self.go("track")
        self.assertIn("Approve a baseline first", page.locator("#track-awaiting").inner_text())
        self.assertTrue(page.locator("#sim-run").is_disabled())
        self.go("intervene")
        self.assertIn("Approve a baseline first", page.locator("#intervene-awaiting").inner_text())

        # The server enforces the same rule, so the UI cannot be bypassed.
        for path in ("/api/simulate", "/api/track/brief", "/api/act/analyze", "/api/act/what-if", "/api/act/apply",
                     "/api/commit/backlog"):
            self.assertEqual(self.post_status(path, {"next_sprint": True}), 409, path)
        self.assertEqual(self.post_status("/api/simulate", {"reset": True}), 200)

        page.get_by_role("button", name="Go to COMMIT").click()
        self.assertEqual(page.locator("#tab-commit").get_attribute("aria-selected"), "true")
        page.get_by_role("button", name="Go to SHAPE").click()
        self.run_shape_to_commit()
        self.assertEqual(self.states()["track"], "Needs approved baseline")
        page.get_by_role("button", name="Approve baseline").click()
        page.locator("#intake-approved").wait_for(state="visible")
        self.assertEqual(self.states()["track"], "Not started")
        self.go("track")
        self.assertTrue(page.locator("#sim-run").is_enabled())
        self.assertEqual(self.post_status("/api/simulate", {"next_sprint": True}), 200)

    def test_demo_data_can_be_cleared_from_the_footer(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        page.evaluate("window.confirm = () => true")
        page.get_by_role("button", name="Clear all demo data").click()
        page.wait_for_function("document.getElementById('clear-data-status').textContent.startsWith('Cleared')")
        self.assertEqual(
            self.states(),
            {"shape": "Start here", "commit": "Needs an estimate",
             "track": "Needs approved baseline", "intervene": "Needs approved baseline"},
        )
        self.go("commit")
        self.assertTrue(page.locator("#commit-empty").is_visible())

    def test_clearing_demo_data_resets_the_page_title_after_a_simulation(self) -> None:
        page = self.page
        self.run_shape_to_commit()
        self.approve_and_simulate()
        page.wait_for_function("document.getElementById('initiative-name').textContent !== 'Delivery Twin'")
        self.assertNotEqual(page.locator("#initiative-name").inner_text(), "Delivery Twin")
        page.evaluate("window.confirm = () => true")
        page.get_by_role("button", name="Clear all demo data").click()
        page.wait_for_function("document.getElementById('clear-data-status').textContent.startsWith('Cleared')")
        page.wait_for_function("document.getElementById('initiative-name').textContent === 'Delivery Twin'")
        self.assertEqual(page.locator("#portfolio-label").text_content(), "Portfolio")
        self.assertEqual(page.locator("#as-of").text_content(), "BEFORE KICKOFF")


if __name__ == "__main__":
    unittest.main()
