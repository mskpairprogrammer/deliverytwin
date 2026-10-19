"""Scenario packs, injected events, file import, baseline history, decision pack, recorded AI and calibration."""
import base64
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from deliverytwin import connect, initialize_database, seed_demo
from deliverytwin.analytics import build_impact_report
from deliverytwin.app import build_report, inject_event, reset_simulation, serve_dashboard, simulate_sprints
from deliverytwin.extras import BaselineRequired, apply_import, baseline_history, load_samples
from deliverytwin.importer import detect_kind, parse_exports
from deliverytwin.intake import analyze_intake, approve_baseline, estimate_intake
from deliverytwin.llm import LLMError, ai_status, complete, openai_configured, openai_model
from deliverytwin.pack import build_pack, clean_review
from deliverytwin.scenarios import SCENARIOS, effective_signals

REQUEST = "We need one view of every customer across the business. Teams get different answers."
DAY = date(2026, 10, 17)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            self.analysis = analyze_intake(self.connection, REQUEST)
        self.answers = {item["id"]: item["suggested"] for item in self.analysis["clarifications"]}

    def tearDown(self) -> None:
        self.connection.close()

    def approve(self, adjustments=None):
        return approve_baseline(
            self.connection, REQUEST, self.answers, DAY, questions=self.analysis["clarifications"], adjustments=adjustments,
        )["baseline"]

    def signals(self):
        return build_report(self.connection)["signals"]


class ScenarioTests(Base):
    def test_the_default_scenario_keeps_the_original_signal_script(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 3)
        signals = self.signals()
        self.assertEqual(
            (signals["unplanned_story_points"], signals["lead_production_support_hours_per_week"],
             signals["oldest_blocking_change_age_days"], signals["active_priority_1_incidents"],
             signals["lead_approved_pto_hours"]),
            (48.0, 12.0, 14, 2, 40),
        )

    def test_scenarios_produce_distinct_forecasts_and_leave_no_stale_rows(self) -> None:
        self.approve()
        confidence = {}
        for key in SCENARIOS:
            simulate_sprints(self.connection, 4, scenario=key)
            report = build_report(self.connection)
            confidence[key] = report["signals"]["unplanned_story_points"], report["data_origin"]["scenario"]
            self.assertEqual(report["data_origin"]["mode"], "synthetic_scenario")
        self.assertEqual(confidence["healthy"], (4.0, "healthy"))
        self.assertGreater(confidence["collision"][0], confidence["healthy"][0])
        # Switching back and forth must give identical results (rows of other scenarios are purged).
        simulate_sprints(self.connection, 4, scenario="collision")
        first = self.signals()
        simulate_sprints(self.connection, 4, scenario="healthy")
        simulate_sprints(self.connection, 4, scenario="collision")
        self.assertEqual(self.signals(), first)

    def test_the_healthy_scenario_has_no_collision_and_the_collision_scenario_does(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 5, scenario="healthy")
        self.assertFalse(build_report(self.connection)["collision_detected"])
        simulate_sprints(self.connection, 5, scenario="collision")
        self.assertTrue(build_report(self.connection)["collision_detected"])

    def test_events_change_the_signals_from_the_current_sprint_on_and_are_cleared_with_the_scenario(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 2, scenario="healthy")
        before = self.signals()
        inject_event(self.connection, "engineer_leaves")
        inject_event(self.connection, "dependency_slip")
        inject_event(self.connection, "p1_incident")
        inject_event(self.connection, "scope_surge")
        inject_event(self.connection, "support_spike")
        after = self.signals()
        self.assertEqual(after["lead_approved_pto_hours"], before["lead_approved_pto_hours"] + 80)
        self.assertEqual(after["oldest_blocking_change_age_days"], 14)
        self.assertEqual(after["active_priority_1_incidents"], before["active_priority_1_incidents"] + 1)
        self.assertEqual(after["unplanned_story_points"], before["unplanned_story_points"] + 16)
        self.assertEqual(after["lead_production_support_hours_per_week"], before["lead_production_support_hours_per_week"] + 12)
        origin = build_report(self.connection)["data_origin"]
        self.assertEqual(len(origin["events"]), 5)
        simulate_sprints(self.connection, 2, scenario="collision")
        self.assertEqual(build_report(self.connection)["data_origin"]["events"], [])
        with self.assertRaises(ValueError):
            inject_event(self.connection, "meteor")

    def test_an_event_needs_a_running_simulation_and_effective_signals_are_pure(self) -> None:
        with self.assertRaises(ValueError):
            inject_event(self.connection, "p1_incident")
        events = [{"sprint_number": 3, "kind": "p1_incident"}]
        self.assertEqual(effective_signals("healthy", 2, events), SCENARIOS["healthy"].plan(2))
        self.assertEqual(effective_signals("healthy", 3, events).p1_incidents, 1)

    def test_resetting_clears_scenario_events_and_simulated_rows(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 3, scenario="incident_spike")
        inject_event(self.connection, "p1_incident")
        reset_simulation(self.connection)
        self.assertIsNone(self.connection.execute("SELECT 1 FROM simulation_state").fetchone())
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM simulation_events").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM ado_work_items WHERE external_id LIKE 'ADO-SIM-%'").fetchone()[0], 0)


def csv_file(name, text):
    return {"name": name, "text": text}


class ImportTests(Base):
    def exports(self):
        return [csv_file(item["name"], item["text"]) for item in load_samples()["exports"]]

    def test_each_kind_of_export_is_detected_from_its_columns(self) -> None:
        kinds = {item["name"]: detect_kind({h for h in (line.lower().replace("_", " ") for line in item["text"].splitlines()[0].split(","))}) for item in load_samples()["exports"]}
        self.assertEqual(kinds, {
            "ado_work_items_export.csv": "ado_work_items", "servicenow_changes_export.csv": "servicenow_changes",
            "servicenow_incidents_export.csv": "servicenow_incidents", "ukg_time_entries_export.csv": "ukg_time_entries",
            "ukg_time_off_export.csv": "ukg_pto",
        })
        self.assertIsNone(detect_kind({"colour", "size"}))

    def test_signals_are_computed_from_the_sample_exports(self) -> None:
        parsed = parse_exports(self.exports(), date.today())
        signals = parsed["signals"]
        self.assertEqual((signals["unplanned_work_item_count"], signals["unplanned_story_points"]), (6, 34.0))
        self.assertEqual(signals["active_priority_1_incidents"], 2)
        self.assertEqual(signals["oldest_blocking_change_age_days"], 24)
        self.assertEqual(signals["lead_production_support_hours_per_week"], 9.0)
        self.assertEqual(signals["lead_approved_pto_hours"], 16.0)
        self.assertEqual(parsed["not_provided"], [])
        self.assertTrue(all(file["accepted"] for file in parsed["files"]))
        ado = next(file for file in parsed["files"] if file["kind"] == "ado_work_items")
        self.assertEqual((ado["rows"], ado["rows_used"], ado["rows_skipped"]), (10, 6, 1))

    def test_partial_and_unusable_files_are_reported_not_guessed(self) -> None:
        parsed = parse_exports([self.exports()[0], csv_file("notes.csv", "colour,size\nred,big\n")], date.today())
        self.assertEqual([f["accepted"] for f in parsed["files"]], [True, False])
        self.assertIn("not recognised", parsed["files"][1]["message"])
        self.assertEqual(len(parsed["not_provided"]), 4)
        with self.assertRaises(ValueError):
            parse_exports([csv_file("notes.csv", "colour,size\nred,big\n")], date.today())
        for bad in (None, [], "x", [csv_file("a.csv", "a,b\n")] * 6, [{"text": "x"}]):
            with self.assertRaises(ValueError):
                parse_exports(bad, date.today())

    def test_a_second_file_of_the_same_kind_is_not_used_and_xlsx_is_read(self) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Number", "Priority", "State", "Opened At"])
        sheet.append(["INC1", "1 - Critical", "New", date.today()])
        buffer = io.BytesIO()
        workbook.save(buffer)
        xlsx = {"name": "incidents.xlsx", "content_base64": base64.b64encode(buffer.getvalue()).decode()}
        incidents = self.exports()[2]
        parsed = parse_exports([xlsx, incidents], date.today())
        self.assertEqual(parsed["signals"]["active_priority_1_incidents"], 1)
        self.assertFalse(parsed["files"][1]["accepted"])
        self.assertIn("already provided", parsed["files"][1]["message"])
        with self.assertRaises(ValueError):
            parse_exports([{"name": "x.xlsx", "content_base64": "not base64!"}], date.today())

    def test_importing_replaces_scripted_signals_and_requires_an_approved_baseline(self) -> None:
        with self.assertRaises(BaselineRequired):
            apply_import(self.connection, self.exports())
        self.approve()
        simulate_sprints(self.connection, 3, scenario="healthy")
        result = apply_import(self.connection, self.exports())
        self.assertTrue(result["imported"])
        report = build_report(self.connection)
        self.assertEqual(report["data_origin"]["mode"], "imported")
        self.assertEqual(report["signals"]["unplanned_story_points"], 34.0)
        self.assertEqual(report["signals"]["blocking_change_requests"][0]["external_id"], "CHG0030011")
        self.assertTrue(report["collision_detected"])
        with self.assertRaises(ValueError):
            inject_event(self.connection, "p1_incident")
        simulate_sprints(self.connection, 2, scenario="healthy")
        self.assertEqual(build_report(self.connection)["data_origin"]["mode"], "synthetic_scenario")

    def test_imported_data_is_a_snapshot_that_cannot_be_advanced(self) -> None:
        from deliverytwin.app import advance_one_sprint

        self.approve()
        apply_import(self.connection, self.exports())
        with self.assertRaisesRegex(ValueError, "snapshot"):
            advance_one_sprint(self.connection)

    def test_pto_counts_only_approved_requests_of_the_lead_under_any_status_header(self) -> None:
        today = date.today().isoformat()
        for header in ("Status", "Request Status"):
            text = (
                f"Time Off Type,Employee,Start Date,End Date,Hours,{header}\n"
                f"Vacation,Lead Engineer,{today},{today},8,Approved\n"
                f"Vacation,Lead Engineer,{today},{today},16,Not Approved\n"
                f"Vacation,Lead Engineer,{today},{today},24,Unapproved\n"
                f"Vacation,Delivery Engineer,{today},{today},32,Approved\n"
            )
            parsed = parse_exports([csv_file("pto.csv", text)], date.today())
            self.assertEqual(parsed["signals"]["lead_approved_pto_hours"], 8.0, header)

    def test_time_entries_of_other_people_are_not_counted_as_the_leads_support_load(self) -> None:
        today = date.today().isoformat()
        text = (f"Employee,Work Date,Pay Code,Hours\nLead Engineer,{today},Production Support,5\n"
                f"Delivery Engineer,{today},Production Support,7\n")
        self.assertEqual(parse_exports([csv_file("t.csv", text)], date.today())["signals"]["lead_production_support_hours_per_week"], 5.0)
        without_people = f"Work Date,Pay Code,Hours\n{today},Production Support,5\n{today},Production Support,7\n"
        self.assertEqual(parse_exports([csv_file("t.csv", without_people)], date.today())["signals"]["lead_production_support_hours_per_week"], 12.0)

    def test_malformed_files_become_rejected_entries_instead_of_crashing(self) -> None:
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<bad")
        broken_xlsx = {"name": "broken.xlsx", "content_base64": base64.b64encode(buffer.getvalue()).decode()}
        huge_cell = csv_file("huge.csv", "a,b\n" + "x" * 200000 + ",1\n")
        parsed = parse_exports([broken_xlsx, huge_cell, self.exports()[2]], date.today())
        self.assertEqual([f["accepted"] for f in parsed["files"]], [False, False, True])
        self.assertIn("could not be read", parsed["files"][0]["message"])
        self.assertIn("could not be read as CSV", parsed["files"][1]["message"])

    def test_an_unknown_scenario_is_rejected_without_losing_injected_events(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 2, scenario="healthy")
        inject_event(self.connection, "p1_incident")
        for bad in ("bogus", ""):
            with self.assertRaises(ValueError):
                simulate_sprints(self.connection, 2, scenario=bad)
        self.assertEqual(len(build_report(self.connection)["data_origin"]["events"]), 1)

    def test_samples_use_dates_relative_to_today(self) -> None:
        text = next(e["text"] for e in load_samples()["exports"] if e["name"].startswith("servicenow_changes"))
        self.assertIn((date.today() - timedelta(days=24)).isoformat(), text)
        self.assertNotIn("{today", text)
        documents = load_samples()["documents"]
        self.assertEqual([d["kind"] for d in documents], ["rom", "meeting_notes"])


class HistoryAndPackTests(Base):
    def test_baseline_history_lists_newest_first_with_deltas(self) -> None:
        self.approve()
        self.approve({"allocated_fte": 8.0})
        history = baseline_history(self.connection)["baselines"]
        self.assertEqual(len(history), 2)
        self.assertIsNone(history[1]["delta_vs_previous"])
        self.assertEqual(history[0]["delta_vs_previous"]["allocated_fte"], round(history[0]["allocated_fte"] - history[1]["allocated_fte"], 2))
        self.assertGreaterEqual(history[0]["approved_at"], history[1]["approved_at"])

    def test_the_pack_needs_a_baseline_and_labels_everything_it_includes(self) -> None:
        self.assertIsNone(build_pack(self.connection))
        approved = self.approve()
        simulate_sprints(self.connection, 2, scenario="dependency_slips")
        review = {"verdict": "ready_with_conditions", "summary": "Tight date | with pipes.", "source": "llm", "model": "gpt-x",
                  "risks": [{"title": "Shared matching", "severity": "high", "detail": "Two slipped."}],
                  "conditions": ["Book a CAB slot."]}
        pack = build_pack(self.connection, review, DAY)
        markdown = pack["markdown"]
        for text in (approved["id"], "AI review (gpt-x)", "Ready with conditions", "Book a CAB slot.",
                     "Dependency slips", "not from a live connection", "Azure DevOps backlog (preview)"):
            self.assertIn(text, markdown)
        self.assertNotIn("date | with", markdown)  # table separators are neutralised
        self.assertTrue(pack["includes_review"])
        self.assertIn("NOT sent", pack["ado_rest"]["note"])
        self.assertFalse(build_pack(self.connection, {"verdict": "bogus"}, DAY)["includes_review"])

    def test_malformed_reviews_are_dropped(self) -> None:
        for bad in (None, "x", {}, {"verdict": "ready"}, {"verdict": "nope", "summary": "x"}):
            self.assertIsNone(clean_review(bad))
        cleaned = clean_review({"verdict": "ready", "summary": "ok", "risks": [{"title": "t", "detail": "d", "severity": "weird"}, 5]})
        self.assertEqual(cleaned["risks"][0]["severity"], "low")
        self.assertEqual(cleaned["source"], "Rules-based review")


class CalibrationTests(Base):
    def test_calibration_reports_coverage_against_the_targets(self) -> None:
        calibration = build_impact_report(self.connection)["calibration"]
        self.assertGreaterEqual(calibration["sample_count"], 20)
        for key in ("p50_coverage_pct", "p80_coverage_pct", "on_time_rate_pct"):
            self.assertTrue(0 <= calibration[key] <= 100, key)
        self.assertGreaterEqual(calibration["p80_coverage_pct"], calibration["p50_coverage_pct"])
        self.assertIn("P80", calibration["verdict"])


class RecordedAiTests(Base):
    def test_recorded_mode_is_labeled_and_drives_every_ai_feature_without_a_key(self) -> None:
        env = {"DELIVERYTWIN_AI_MODE": "recorded", "OPENAI_API_KEY": "", "AZURE_OPENAI_ENDPOINT": ""}
        with patch.dict(os.environ, env):
            self.assertEqual(ai_status()["mode"], "recorded")
            self.assertTrue(openai_configured())
            self.assertEqual(openai_model(), "recorded-sample")
            analysis = analyze_intake(self.connection, REQUEST)
            self.assertEqual(analysis["question_source"]["type"], "llm")
            self.assertEqual(analysis["question_source"]["model"], "recorded-sample")
            answers = {item["id"]: item["suggested"] for item in analysis["clarifications"]}
            result = estimate_intake(self.connection, REQUEST, answers, DAY, questions=analysis["clarifications"])
            self.assertEqual(result["prediction"]["source"], "llm")
            self.assertEqual(result["prediction"]["model"], "recorded-sample")
            from deliverytwin.commit_ai import ask_whatif, challenge_commitment
            from deliverytwin.intake import evaluate_intake

            evaluation = evaluate_intake(self.connection, REQUEST, answers, DAY, questions=analysis["clarifications"], basis=result["basis"])
            review = challenge_commitment(self.connection, evaluation)
            self.assertEqual((review["source"], review["model"]), ("llm", "recorded-sample"))
            ask = ask_whatif(self.connection, "What if we lose an engineer?", DAY,
                             lambda adj: evaluate_intake(self.connection, REQUEST, answers, DAY, adjustments=adj,
                                                         questions=analysis["clarifications"], basis=result["basis"]), {})
            self.assertTrue(ask["answerable"])
            self.assertEqual(ask["scenario"]["allocated_fte"], ask["current"]["allocated_fte"] - 1)

    def test_recorded_track_and_recovery_answers_pass_validation(self) -> None:
        self.approve()
        simulate_sprints(self.connection, 3)
        from deliverytwin.app import build_workflow_report
        from deliverytwin.intake import latest_baseline
        from deliverytwin.recovery import analyze_intervention
        from deliverytwin.track_ai import build_brief

        with patch.dict(os.environ, {"DELIVERYTWIN_AI_MODE": "recorded"}):
            report = build_workflow_report(self.connection)
            brief = build_brief(report, latest_baseline(self.connection))
            self.assertEqual((brief["source"], brief["model"]), ("llm", "recorded-sample"))
            late = [dict(row) for row in self.connection.execute(
                "SELECT project_name, planned_effort_hours, actual_effort_hours, planned_duration_weeks, "
                "actual_duration_weeks, outcome, lessons_learned FROM historical_projects LIMIT 3")]
            analysis = analyze_intervention(report, late)
            self.assertEqual((analysis["source"], analysis["model"]), ("llm", "recorded-sample"))
            self.assertEqual(len(analysis["scenarios"]), 3)

    def test_an_unknown_prompt_has_no_recorded_answer(self) -> None:
        with patch.dict(os.environ, {"DELIVERYTWIN_AI_MODE": "recorded"}):
            with self.assertRaises(LLMError):
                complete([{"role": "system", "content": "something else"}, {"role": "user", "content": "{}"}])

    def test_off_when_nothing_is_configured(self) -> None:
        env = {"DELIVERYTWIN_AI_MODE": "", "OPENAI_API_KEY": "", "AZURE_OPENAI_ENDPOINT": "", "AZURE_OPENAI_DEPLOYMENT": ""}
        with patch.dict(os.environ, env):
            self.assertEqual(ai_status(), {"mode": "off", "model": None, "label": ai_status()["label"]})
            self.assertFalse(openai_configured())


class AzureOpenAiTests(unittest.TestCase):
    ENV = {"DELIVERYTWIN_AI_MODE": "", "OPENAI_API_KEY": "sk-unused", "AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/",
           "AZURE_OPENAI_DEPLOYMENT": "gpt-4o-mini-dep", "AZURE_OPENAI_API_KEY": "azure-key"}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def test_azure_takes_precedence_and_calls_the_deployment_url_with_its_key(self) -> None:
        seen = {}

        def fake_urlopen(request, timeout):
            seen.update(url=request.full_url, headers=dict(request.header_items()), body=json.loads(request.data))
            return self.Response(json.dumps({"choices": [{"message": {"content": "{\"ok\": true}"}}]}).encode())

        with patch.dict(os.environ, self.ENV), patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(ai_status(), {"mode": "azure_openai", "model": "gpt-4o-mini-dep", "label": "Azure OpenAI"})
            self.assertEqual(complete([{"role": "user", "content": "hi"}], json_mode=True), "{\"ok\": true}")
        self.assertEqual(
            seen["url"], "https://example.openai.azure.com/openai/deployments/gpt-4o-mini-dep/chat/completions?api-version=2024-10-21")
        self.assertEqual(seen["headers"]["Api-key"], "azure-key")
        self.assertNotIn("Authorization", seen["headers"])
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})

    def test_an_insecure_endpoint_is_ignored_and_failures_become_llm_errors(self) -> None:
        with patch.dict(os.environ, {**self.ENV, "AZURE_OPENAI_ENDPOINT": "http://example.com"}):
            self.assertEqual(ai_status()["mode"], "openai")
        import urllib.error

        def failing(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 429, "busy", {}, None)

        with patch.dict(os.environ, self.ENV), patch("urllib.request.urlopen", failing):
            with self.assertRaises(LLMError) as raised:
                complete([{"role": "user", "content": "hi"}])
        self.assertIn("HTTP 429", str(raised.exception))


class RouteGuardTests(unittest.TestCase):
    def serve(self, env):
        port = free_port()
        database = os.path.join(tempfile.mkdtemp(), "routes.db")
        with patch.dict(os.environ, env):
            for name in ("DELIVERYTWIN_DEMO_MODE", "DELIVERYTWIN_SIMULATION"):
                if name not in env:
                    os.environ.pop(name, None)
            threading.Thread(target=serve_dashboard, args=(database, "127.0.0.1", port), daemon=True).start()
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
        return port

    @staticmethod
    def request(port, method, route, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(method, route, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        response = connection.getresponse()
        data = json.loads(response.read() or b"{}")
        connection.close()
        return response.status, data

    def test_simulation_can_be_enabled_without_the_data_wiping_demo_mode(self) -> None:
        port = self.serve({"DELIVERYTWIN_DEMO_MODE": "0", "DELIVERYTWIN_SIMULATION": "1"})
        self.assertEqual(self.request(port, "POST", "/api/reset-all", {"confirm": True})[0], 403)
        self.assertEqual(self.request(port, "POST", "/api/simulate", {"reset": True})[0], 403)
        self.assertEqual(self.request(port, "POST", "/api/simulate", {"next_sprint": True})[0], 409)  # baseline needed
        self.assertEqual(self.request(port, "POST", "/api/simulate/event", {"kind": "p1_incident"})[0], 409)
        self.assertEqual(self.request(port, "POST", "/api/import/signals", {"files": []})[0], 409)

    def test_simulation_routes_are_blocked_when_neither_flag_is_set(self) -> None:
        port = self.serve({"DELIVERYTWIN_DEMO_MODE": "0"})
        for route in ("/api/simulate", "/api/simulate/event", "/api/import/signals"):
            self.assertEqual(self.request(port, "POST", route, {})[0], 403, route)

    def test_read_only_routes_describe_the_environment(self) -> None:
        port = self.serve({"DELIVERYTWIN_DEMO_MODE": "1"})
        status, data = self.request(port, "GET", "/api/ai/status")
        self.assertEqual(status, 200)
        self.assertIn(data["mode"], {"off", "openai", "azure_openai", "recorded"})
        status, data = self.request(port, "GET", "/api/simulate/catalog")
        self.assertEqual((status, data["scenario"], [s["key"] for s in data["scenarios"]]), (200, None, list(SCENARIOS)))
        self.assertEqual(self.request(port, "GET", "/api/baselines")[1], {"baselines": []})
        status, data = self.request(port, "GET", "/api/samples")
        self.assertEqual((status, len(data["documents"]), len(data["exports"])), (200, 2, 5))
        self.assertEqual(self.request(port, "POST", "/api/commit/pack", {})[0], 409)


if __name__ == "__main__":
    unittest.main()
