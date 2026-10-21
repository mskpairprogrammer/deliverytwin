"""P0 portfolio preemption: signal, swimlane, portfolio-aware recovery options and sprint re-planning."""
from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from deliverytwin.app import (
    _latest_recovery_scenario,
    build_workflow_report,
    initialize_database,
    reset_simulation,
    save_recovery_scenario,
    seed_demo,
    simulate_sprints,
    trigger_preemption,
)
from deliverytwin.backlog import compare_plans, latest_backlog, plan_sprints
from deliverytwin.data.connection import connect
from deliverytwin.intake import approve_baseline
from deliverytwin.portfolio import (
    PI_SPRINTS,
    PREEMPT_SPRINTS,
    capacity_vector,
    donor_impact,
    planned_loss_hours,
    preempted_hours_so_far,
    preemption_active,
)
from deliverytwin.recovery import analyze_intervention, predict_scenario

ANSWERS = {"architecture": "incremental", "data": "event_sync", "security": "pii_consent", "reuse": "reuse_mdm"}
REQUEST = "We need one view of every customer across the business."


class PreemptionTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in ("deliverytwin.intake.openai_configured", "deliverytwin.recovery.openai_configured"):
            patcher = patch(target, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        initialize_database(self.connection)
        today = date.today()
        seed_demo(self.connection, today)
        approve_baseline(
            self.connection, REQUEST, ANSWERS, today,
            adjustments={"allocated_fte": 7.0, "target_date": today.replace(year=today.year + 1).isoformat(),
                         "scope_change_pct": 0},
        )
        simulate_sprints(self.connection, 2, today, scenario="healthy")

    def report(self) -> dict:
        report = build_workflow_report(self.connection)
        if report["portfolio"]["active"]:
            report["portfolio"]["backlog"] = latest_backlog(self.connection)
        return report

    def test_the_preemption_moves_the_simulation_to_sprint_three_and_lowers_the_forecast(self) -> None:
        before = self.report()
        self.assertFalse(before["portfolio"]["active"])
        self.assertEqual(before["signals"]["portfolio_preempted_hours"], 0)
        trigger_preemption(self.connection)
        self.assertTrue(preemption_active(self.connection))
        after = self.report()
        self.assertEqual(after["simulation"]["sprints_advanced"], PREEMPT_SPRINTS[0])
        # The forecast prices the whole announced loss: 400 planned hours at 0.12 points each = 48 points.
        self.assertEqual(after["signals"]["portfolio_preempted_hours"], 400)
        trigger_preemption(self.connection, False)
        without = self.report()["workflow"]["stages"]["track"]["on_time_pct"]
        trigger_preemption(self.connection)
        preempted = self.report()["workflow"]["stages"]["track"]["on_time_pct"]
        self.assertEqual(without - preempted, 48)
        self.assertGreater(without, 80)
        self.assertLess(preempted, 50)
        self.assertEqual([preempted_hours_so_far(n) for n in (2, 3, 4, 6)], [0, 200, 400, 400])

    def test_the_preemption_is_cleared_by_reset_and_by_switching_scenario(self) -> None:
        trigger_preemption(self.connection)
        simulate_sprints(self.connection, 3, date.today(), scenario="collision")
        self.assertFalse(preemption_active(self.connection))
        trigger_preemption(self.connection)
        reset_simulation(self.connection)
        self.assertFalse(preemption_active(self.connection))

    def test_the_swimlane_shows_the_p0_and_sibling_projects_with_the_collision_flagged(self) -> None:
        trigger_preemption(self.connection)
        view = self.report()["portfolio"]
        self.assertEqual(view["sprints"], list(range(1, PI_SPRINTS + 1)))
        projects = {project["id"]: project for project in view["projects"]}
        self.assertEqual({p["tier"] for p in projects.values()}, {"P1", "P0", "P2", "P3"})
        self.assertEqual(projects["P0-REG-2026"]["name"], "Urgent Regulatory Compliance")
        status = lambda project: [cell["status"] for cell in project["cells"]]
        self.assertEqual(status(projects["CURRENT"]), ["ok", "ok", "collision", "collision", "ok", "ok"])
        self.assertEqual(status(projects["P0-REG-2026"]), ["idle", "idle", "collision", "collision", "idle", "idle"])
        self.assertEqual(status(projects["PRJ-024"]), ["ok", "ok", "watch", "watch", "ok", "ok"])
        self.assertEqual([c["effective_fte"] for c in projects["CURRENT"]["cells"]], [7.0, 7.0, 4.5, 4.5, 7.0, 7.0])
        self.assertEqual(projects["P0-REG-2026"]["cells"][2]["planned_fte"], 2.5)

    def test_portfolio_recovery_options_are_deterministic_and_the_composed_plan_is_recommended(self) -> None:
        trigger_preemption(self.connection)
        analysis = analyze_intervention(self.report(), [])
        self.assertEqual(analysis["source"], "portfolio_rules")
        by_id = {s["id"]: s for s in analysis["scenarios"]}
        self.assertEqual(set(by_id), {"resequence", "cross_portfolio", "backfill", "hybrid_replan"})
        self.assertEqual(by_id["resequence"]["cost_impact"]["label"], "$0")
        self.assertEqual(by_id["cross_portfolio"]["cost_impact"]["label"], "$0")
        self.assertEqual(by_id["backfill"]["cost_impact"]["label"], "+$60,000")
        self.assertEqual(by_id["hybrid_replan"]["cost_impact"]["label"], "+$12,000")
        self.assertEqual(by_id["hybrid_replan"]["levers"], {
            "deferred_hours": 120, "borrowed_fte": 1.5, "backfill_fte": 0.5,
            "reclaimed_hours": 400, "residual_hours": 0, "cost_usd": 12000,
        })
        self.assertEqual(planned_loss_hours(), 400)
        self.assertEqual(by_id["resequence"]["levers"], {
            "deferred_hours": 400, "borrowed_fte": 0.0, "backfill_fte": 0.0,
            "reclaimed_hours": 400, "residual_hours": 0, "cost_usd": 0,
        })
        self.assertIsNone(by_id["resequence"]["donor_impact"])
        self.assertEqual(analysis["scenarios"][0]["id"], "hybrid_replan")
        self.assertGreater(by_id["hybrid_replan"]["predicted_on_time_pct"], by_id["cross_portfolio"]["predicted_on_time_pct"])
        self.assertEqual(by_id["cross_portfolio"]["predicted_on_time_pct"], by_id["backfill"]["predicted_on_time_pct"])
        # Deferring non-critical scope costs less confidence than leaving critical preempted hours uncovered.
        self.assertGreater(by_id["resequence"]["predicted_on_time_pct"], by_id["backfill"]["predicted_on_time_pct"])
        # Every portfolio recovery option brings the forecast back above 70%; the composed plan to about 80%.
        self.assertTrue(all(s["predicted_on_time_pct"] > 70 for s in analysis["scenarios"]))
        self.assertGreaterEqual(by_id["hybrid_replan"]["predicted_on_time_pct"], 78)
        self.assertLess(analysis["portfolio"]["unmanaged_confidence_pct"], 50)
        self.assertEqual(analysis, analyze_intervention(self.report(), []))

    def test_borrowing_from_the_p3_project_shows_its_trade_off_and_respects_its_floor(self) -> None:
        trigger_preemption(self.connection)
        analysis = analyze_intervention(self.report(), [])
        borrow = next(s for s in analysis["scenarios"] if s["id"] == "cross_portfolio")
        impact = borrow["donor_impact"]
        self.assertEqual((impact["project_id"], impact["tier"], impact["borrowed_fte"]), ("PRJ-024", "P3", 2.0))
        self.assertEqual((impact["fte_after"], impact["lost_hours"], impact["slip_weeks"]), (2.0, 320, 2.0))
        self.assertTrue(any("PRJ-024" in text for text in borrow["trade_offs"]))
        for scenario in analysis["scenarios"]:
            if scenario["donor_impact"]:
                self.assertGreaterEqual(scenario["donor_impact"]["fte_after"], scenario["donor_impact"]["floor_fte"])
        self.assertIsNone(donor_impact(0))

    def test_sprint_plans_conserve_capacity_and_hours(self) -> None:
        trigger_preemption(self.connection)
        backlog = latest_backlog(self.connection)
        total = sum(f["effort_hours"] for f in backlog["epic"]["features"])
        self.assertEqual(capacity_vector(7.0, preempted=False), [560] * 6)
        self.assertEqual(capacity_vector(7.0), [560, 560, 360, 360, 560, 560])
        self.assertEqual(capacity_vector(7.0, 2.0), [560, 560, 520, 520, 560, 560])
        for capacity, deferred in ((capacity_vector(7.0), 0), (capacity_vector(7.0, 2.0), 0), (capacity_vector(7.0, 1.5), 120)):
            plan = plan_sprints(backlog, capacity, deferred)
            for index, cap in enumerate(capacity):
                self.assertLessEqual(sum(row["hours_by_sprint"][index] for row in plan["features"]), cap)
            worked = sum(sum(row["hours_by_sprint"]) for row in plan["features"])
            self.assertEqual(worked + sum(row["next_pi_hours"] for row in plan["features"]), total)
            self.assertEqual(plan, plan_sprints(backlog, capacity, deferred))

    def test_re_sequencing_defers_only_non_critical_features_and_the_plan_marks_what_moved(self) -> None:
        trigger_preemption(self.connection)
        backlog = latest_backlog(self.connection)
        baseline = plan_sprints(backlog, capacity_vector(7.0, preempted=False))
        preempted = compare_plans(baseline, plan_sprints(backlog, capacity_vector(7.0)))
        resequenced = compare_plans(baseline, plan_sprints(backlog, capacity_vector(7.0), 120))
        base_next = sum(row["next_pi_hours"] for row in preempted["features"])
        # Capacity is conserved: deferring 120 h of one Feature lets 120 h of later work fit, so Next PI is unchanged.
        self.assertEqual(sum(row["next_pi_hours"] for row in resequenced["features"]), base_next)
        deferred_rows = [row for row in resequenced["features"] if row["status"] in ("next_pi", "partly_next_pi")]
        self.assertTrue(deferred_rows)
        self.assertTrue(all(not row["critical"] for row in deferred_rows))
        self.assertGreaterEqual(preempted["summary"]["shifted"] + preempted["summary"]["moved_to_next_pi"], 1)
        self.assertTrue(all(row["status"] in {"unchanged", "shifted", "pulled_in", "next_pi", "partly_next_pi"}
                            for row in preempted["features"]))

    def test_previewing_and_applying_a_portfolio_scenario_is_deterministic_and_updates_the_backlog(self) -> None:
        trigger_preemption(self.connection)
        report = self.report()
        chosen = analyze_intervention(report, [])["scenarios"][0]
        preview = predict_scenario(report, {"portfolio_option": "hybrid_replan"}, [])
        self.assertEqual(preview["confidence_pct"], chosen["predicted_on_time_pct"])
        self.assertEqual(preview["cost_impact"]["label"], "+$12,000")
        self.assertEqual(preview["prediction"]["source"], "portfolio_rules")
        self.assertEqual(preview["recommended_option"], "hybrid_replan")
        self.assertFalse(preview["writeback_performed"])
        self.assertEqual(preview["pi_plan"]["summary"], chosen["pi_plan"]["summary"])
        saved = save_recovery_scenario(self.connection, preview)
        self.assertEqual(saved["recommended_option"], "hybrid_replan")
        self.assertEqual(_latest_recovery_scenario(self.connection)["recommended_option"], "hybrid_replan")
        plan = latest_backlog(self.connection)["plan"]
        self.assertIn("Deferred scope: follow-on release", [f["title"] for f in plan["epic"]["features"]])
        with self.assertRaises(ValueError):
            predict_scenario(report, {"portfolio_option": "nope"}, [])

    def test_without_a_preemption_the_recovery_analysis_is_unchanged(self) -> None:
        analysis = analyze_intervention(self.report(), [])
        self.assertEqual(analysis["source"], "rules")
        with self.assertRaises(ValueError):
            predict_scenario(self.report(), {"portfolio_option": "hybrid_replan"}, [])


if __name__ == "__main__":
    unittest.main()
