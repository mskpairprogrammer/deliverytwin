"""Dual-dialect workflow, quote verification, dollar-cost, Role x Phase guardrail and ROI contracts."""
from __future__ import annotations

import json
import os
import threading
import unittest
from datetime import date
from unittest.mock import patch

from deliverytwin.analytics import build_impact_report, session_dependency_detection
from deliverytwin.app import (
    _latest_recovery_scenario,
    build_workflow_report,
    clear_demo_data,
    initialize_database,
    main,
    record_track_snapshot,
    save_recovery_scenario,
    seed_demo,
    simulate_sprints,
)
from deliverytwin.capacity import GUARDRAIL_MESSAGE, MIN_GROUP_SIZE, PHASES, role_family
from deliverytwin.storage import BaselineRepository, TrackSnapshotRepository
from deliverytwin.storage.connection import connect, resolve_dialect
from deliverytwin.intake import approve_baseline, evaluate_intake
from deliverytwin.provenance import attribute, normalize, verify_quote
from deliverytwin.recovery import (
    analyze_intervention,
    cost_impact,
    format_usd,
    predict_scenario,
    rank_scenarios,
)

START = date(2026, 2, 20)
REQUEST = "We need one view of every customer. Teams get different answers."
ANSWERS = {"architecture": "incremental", "data": "event_sync", "security": "pii_consent", "reuse": "reuse_mdm"}
DIALECTS = ("duckdb",)


class WorkflowOnEveryDialectTests(unittest.TestCase):
    """SHAPE -> COMMIT -> TRACK -> INTERVENE, touching all four application tables, on DuckDB."""

    def test_full_workflow_and_all_four_app_tables(self) -> None:
        for dialect in DIALECTS:
            with self.subTest(dialect=dialect), patch("deliverytwin.intake.openai_configured", return_value=False), \
                    patch("deliverytwin.recovery.openai_configured", return_value=False):
                connection = connect(":memory:", dialect=dialect)
                self.addCleanup(connection.close)
                initialize_database(connection)
                seed_demo(connection, START)

                # SHAPE + COMMIT: commitment_baselines
                approved = approve_baseline(
                    connection, REQUEST, ANSWERS, START,
                    adjustments={"allocated_fte": 7.0, "target_date": "2026-09-30", "scope_change_pct": 0},
                )
                baselines = BaselineRepository(connection.app_database())
                self.assertEqual(baselines.latest()["p80_hours"], approved["commitment"]["p80_hours"])
                self.assertEqual(len(baselines.history()), 1)

                # intake_sessions are recorded by the seeded activity
                self.assertTrue(
                    connection.app_database().query("SELECT COUNT(*) AS n FROM "
                                                    + connection.app_database().table("dt_intake_sessions"))[0]["n"]
                )

                # TRACK: track_snapshots
                simulate_sprints(connection, 2, START)
                report = build_workflow_report(connection)
                trend = record_track_snapshot(connection, report)
                self.assertEqual(len(trend), 1)
                self.assertEqual(len(record_track_snapshot(connection, report)), 1, "idempotent per sprint")

                # INTERVENE: recovery_scenarios (+ target updates) and the dollar impact
                analysis = analyze_intervention(report, [])
                self.assertEqual(analysis["source"], "rules")
                self.assertEqual(analysis["recommended"], analysis["scenarios"][0]["id"])
                payload = {key: analysis["scenarios"][0][key] for key in
                           ("allocated_fte", "target_date", "scope_defer_pct", "incremental_cost_k")}
                saved = save_recovery_scenario(connection, predict_scenario(report, payload, []))
                self.assertEqual(saved["status"], "simulated_updates_persisted")
                self.assertTrue(saved["target_updates"])
                self.assertIsNotNone(_latest_recovery_scenario(connection))

                # The same rows are visible through the repositories, then everything resets.
                self.assertEqual(len(TrackSnapshotRepository(connection.app_database()).trend(
                    report["initiative"]["id"])), 1)
                clear_demo_data(connection)
                self.assertIsNone(baselines.latest())

    def test_dialect_resolution(self) -> None:
        self.assertEqual(resolve_dialect(None, {}), "duckdb")
        self.assertEqual(resolve_dialect("duckdb", {}), "duckdb")
        self.assertEqual(resolve_dialect(None, {"DELIVERYTWIN_DB_DIALECT": "duckdb"}), "duckdb")
        with self.assertRaises(ValueError):
            resolve_dialect("sqlite", {})
        with self.assertRaises(ValueError):
            resolve_dialect(None, {"DELIVERYTWIN_DB_DIALECT": "sqlite"})
        self.assertEqual(resolve_dialect(None, {"DATABRICKS_HOST": "https://adb-1.azuredatabricks.net"}), "databricks")
        self.assertEqual(resolve_dialect("duckdb", {"DATABRICKS_HOST": "https://adb-1.azuredatabricks.net"}), "duckdb")

    def test_cli_accepts_a_dialect_flag_on_every_command(self) -> None:
        for dialect in DIALECTS:
            with self.subTest(dialect=dialect), patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                self.assertEqual(main(["--db", ":memory:", "init", "--dialect", dialect]), 0)
        for command in ("init", "seed-demo", "report", "demo", "simulate", "serve"):
            with self.subTest(command=command), self.assertRaises(SystemExit):
                main([command, "--dialect", "sqlite"])


class DuckDbThreadSafetyTests(unittest.TestCase):
    def test_concurrent_requests_share_one_file_without_colliding(self) -> None:
        import tempfile
        from pathlib import Path

        from deliverytwin.storage import DuckDBDatabase, TrackSnapshotRepository

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "deliverytwin.duckdb")
            DuckDBDatabase(path).create_tables()
            errors: list[BaseException] = []

            def worker(index: int) -> None:
                try:
                    repository = TrackSnapshotRepository(DuckDBDatabase(path))
                    for sprint in range(10):
                        repository.record({
                            "initiative_id": f"init-{index}", "sprint_number": sprint, "as_of": "2026-03-01",
                            "on_time_pct": 50, "projected_slip_days": 3, "unplanned_story_points": 1,
                            "support_hours_per_week": 2,
                        })
                        repository.trend(f"init-{index}")
                except BaseException as error:  # noqa: BLE001 - surfaced by the assertion below
                    errors.append(error)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            repository = TrackSnapshotRepository(DuckDBDatabase(path))
            self.assertTrue(all(len(repository.trend(f"init-{index}")) == 10 for index in range(8)))


class QuoteVerifierTests(unittest.TestCase):
    NOTES = {"name": "notes.txt", "kind": "meeting_notes",
             "text": "Priya:  The privacy review\nusually takes six weeks before we can ship."}
    REQUEST = "Customers must be able to opt out of marketing messages within the app."

    def test_verbatim_quote_is_normalised_for_case_and_whitespace(self) -> None:
        self.assertEqual(normalize("  The  PRIVACY\nreview "), "the privacy review")
        self.assertTrue(verify_quote("THE privacy review usually takes", [self.NOTES["text"]]))
        self.assertFalse(verify_quote("the privacy review usually takes eight weeks", [self.NOTES["text"]]))
        self.assertFalse(verify_quote("privacy review", [self.NOTES["text"]]), "needs at least three words")

    def test_verified_document_claims_keep_their_badge(self) -> None:
        item = {"source": "document", "source_document": "notes.txt",
                "source_quote": "privacy review usually takes six weeks"}
        badge = attribute(item, self.REQUEST, [self.NOTES])
        self.assertEqual(badge["label"], "Found in Meeting Notes (Quote Verified)")
        self.assertTrue(badge["verified"])

    def test_failed_or_missing_quotes_are_downgraded_to_ai_inference(self) -> None:
        wrong = attribute({"source": "document", "source_quote": "the privacy review takes two days"},
                          self.REQUEST, [self.NOTES])
        missing = attribute({"source": "document"}, self.REQUEST, [self.NOTES])
        no_document = attribute({"source": "document", "source_quote": "privacy review usually takes"},
                                self.REQUEST, [])
        for badge in (wrong, missing, no_document):
            self.assertEqual(badge["label"], "AI Inference (Confirm)")
            self.assertFalse(badge["verified"])
            self.assertTrue(badge["downgrade_reason"])

    def test_request_claims_are_verified_against_the_request_text(self) -> None:
        good = attribute({"source": "request", "source_quote": "opt out of marketing messages"}, self.REQUEST, [])
        bad = attribute({"source": "request", "source_quote": "opt out of every channel"}, self.REQUEST, [])
        self.assertEqual(good["label"], "Stated in Request")
        self.assertEqual(bad["label"], "AI Inference (Confirm)")


class CostImpactTests(unittest.TestCase):
    REPORT = {"as_of": "2026-03-01", "initiative": {"target_end": "2026-06-01"}, "signals": {"allocated_fte": 5.0}}

    def test_cost_is_deterministic_from_the_three_levers(self) -> None:
        result = cost_impact(self.REPORT, 7.0, date(2026, 7, 1), 50, rate=150)
        self.assertEqual(result["components"], {
            "added_capacity_usd": 209143, "extension_usd": 180000, "budget_usd": 50000,
        })
        self.assertEqual((result["usd"], result["label"]), (439143, "+$439,143"))
        self.assertEqual(result["rate_per_hour"], 150)
        self.assertEqual(result, cost_impact(self.REPORT, 7.0, date(2026, 7, 1), 50, rate=150))

    def test_no_change_costs_zero_and_scope_deferral_is_not_a_saving(self) -> None:
        result = cost_impact(self.REPORT, 5.0, date(2026, 6, 1), 0, rate=150)
        self.assertEqual((result["usd"], result["label"]), (0, "$0"))
        self.assertEqual(format_usd(0), "$0")
        self.assertEqual(format_usd(18000), "+$18,000")

    def test_rate_is_configurable_and_defaults_to_150(self) -> None:
        with patch.dict(os.environ, {"DELIVERYTWIN_BLENDED_RATE": "200"}):
            self.assertEqual(cost_impact(self.REPORT, 6.0, date(2026, 6, 1), 0)["rate_per_hour"], 200)
        with patch.dict(os.environ, {"DELIVERYTWIN_BLENDED_RATE": "oops"}):
            self.assertEqual(cost_impact(self.REPORT, 6.0, date(2026, 6, 1), 0)["rate_per_hour"], 150)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DELIVERYTWIN_BLENDED_RATE", None)
            self.assertEqual(cost_impact(self.REPORT, 6.0, date(2026, 6, 1), 0)["rate_per_hour"], 150)

    def test_ranking_is_confidence_then_cost_then_date(self) -> None:
        def scenario(identifier, pct, usd, day):
            return {"id": identifier, "predicted_on_time_pct": pct, "cost_impact": {"usd": usd}, "target_date": day}

        ranked = rank_scenarios([
            scenario("cheap_low", 70, 0, "2026-06-01"),
            scenario("late_costly", 80, 5000, "2026-09-01"),
            scenario("early_costly", 80, 5000, "2026-07-01"),
            scenario("cheap_high", 80, 1000, "2026-12-01"),
        ])
        self.assertEqual([s["id"] for s in ranked], ["cheap_high", "early_costly", "late_costly", "cheap_low"])


class RolePhaseGuardrailTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch("deliverytwin.intake.openai_configured", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        initialize_database(self.connection)
        seed_demo(self.connection, START)
        self.result = evaluate_intake(
            self.connection, REQUEST, ANSWERS, START,
            adjustments={"allocated_fte": 7.0, "target_date": "2026-09-30", "scope_change_pct": 0},
        )

    def test_breakdown_covers_four_roles_by_four_phases_and_is_deterministic(self) -> None:
        breakdown = self.result["role_phase"]
        self.assertEqual(breakdown["phases"], list(PHASES))
        self.assertEqual(len(breakdown["roles"]), 4)
        self.assertTrue(all(len(role["cells"]) == 4 for role in breakdown["roles"]))
        total = sum(cell["required_hours"] for role in breakdown["roles"] for cell in role["cells"])
        self.assertAlmostEqual(total, self.result["commitment"]["p80_hours"], delta=16)
        again = evaluate_intake(
            self.connection, REQUEST, ANSWERS, START,
            adjustments={"allocated_fte": 7.0, "target_date": "2026-09-30", "scope_change_pct": 0},
        )
        self.assertEqual(again["role_phase"], breakdown)

    def test_an_unstaffed_role_is_called_out(self) -> None:
        qa = next(role for role in self.result["role_phase"]["roles"] if role["key"] == "qa")
        self.assertEqual(qa["planned_fte"], 0)
        self.assertTrue(all(cell["status"] == "unstaffed" for cell in qa["cells"]))
        self.assertIn("QA", {item["role"] for item in self.result["role_phase"]["bottlenecks"]})

    def test_guardrail_message_and_no_worker_level_data(self) -> None:
        guardrail = self.result["role_phase"]["privacy_guardrail"]
        self.assertEqual(guardrail["message"], GUARDRAIL_MESSAGE)
        self.assertTrue(guardrail["active"])
        self.assertEqual(guardrail["min_group_size"], MIN_GROUP_SIZE)
        # Every demo role group has fewer than three workers, so each is suppressed rather than reported.
        self.assertTrue(guardrail["role_groups"])
        self.assertTrue(all(group["suppressed"] and group["workers"] is None for group in guardrail["role_groups"]))
        serialised = json.dumps(self.result["role_phase"])
        for row in self.connection.execute("SELECT id, display_name FROM dt_people"):
            self.assertNotIn(row["display_name"], serialised)
            self.assertNotIn(row["id"], serialised)

    def test_role_families_map_planview_roles(self) -> None:
        self.assertEqual(role_family("Customer Data Engineer"), "data")
        self.assertEqual(role_family("Customer Privacy Engineer"), "security")
        self.assertEqual(role_family("CRM Integration Engineer"), "integration")
        self.assertEqual(role_family("QA Lead"), "qa")


class RoiMetricTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        initialize_database(self.connection)
        seed_demo(self.connection, START)

    def test_impact_report_carries_both_deterministic_roi_metrics(self) -> None:
        roi = build_impact_report(self.connection)["roi"]
        early, variance = roi["early_dependency_detection"], roi["estimator_variance"]
        self.assertEqual((early["projects"], early["dependencies"]), (50, 90))
        self.assertEqual(early["recall_pct"], round(early["caught"] / early["dependencies"] * 100))
        self.assertIn("No requester input", early["method"])
        self.assertIn("no mid-flight baseline", early["baseline_note"])
        self.assertGreater(variance["original_plan_error_sd_pts"], 0)
        expected = round((variance["original_plan_error_sd_pts"] - variance["analog_error_sd_pts"])
                         / variance["original_plan_error_sd_pts"] * 100)
        self.assertEqual(variance["spread_reduction_pct"], expected)
        self.assertEqual(build_impact_report(self.connection)["roi"], roi)

    def test_per_request_detection_separates_flagged_from_stated(self) -> None:
        matches = [{"dimensions": {"dependencies": {"historical": ["privacy-review", "consent-management"]}}}] * 3
        fingerprint = {"dependencies": ["privacy-review", "crm-mdm-integration"]}
        quiet = session_dependency_detection("One view of every customer.", fingerprint, matches)
        self.assertEqual(quiet["flagged"], ["consent-management", "crm-mdm-integration", "privacy-review"])
        self.assertEqual((quiet["flagged_count"], quiet["stated_count"]), (3, 0))
        loud = session_dependency_detection("Needs a privacy review and CRM changes.", fingerprint, matches)
        self.assertEqual(loud["stated_in_request"], ["crm-mdm-integration", "privacy-review"])
        self.assertEqual(loud["surfaced_by_shape"], 1)


class DatabricksAppsConfigTests(unittest.TestCase):
    def test_app_yaml_declares_the_serve_command_and_databricks_environment(self) -> None:
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "src" / "app.yaml").read_text(encoding="utf-8")
        self.assertIn('command: ["python", "-m", "deliverytwin", "serve"]', text)
        for name in ("DATABRICKS_HOST", "DATABRICKS_HTTP_PATH", "DELIVERYTWIN_CATALOG", "DELIVERYTWIN_SCHEMA",
                     "DATABRICKS_SERVING_ENDPOINT", "DELIVERYTWIN_TRUSTED_AUTH"):
            self.assertIn(f"- name: {name}", text)
        self.assertRegex(text, r"name: DELIVERYTWIN_DB_DIALECT\s+value: \"databricks\"")
        self.assertNotIn("DATABRICKS_TOKEN", text)
        self.assertNotRegex(text, r"sk-[A-Za-z0-9]{10,}|dapi[0-9a-f]{10,}")


if __name__ == "__main__":
    unittest.main()
