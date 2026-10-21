"""Tuning a recovery what-if from a plain-English instruction: the AI proposes levers, code scores them."""
from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from deliverytwin.app import (
    _latest_recovery_scenario,
    build_workflow_report,
    initialize_database,
    save_recovery_scenario,
    seed_demo,
    simulate_sprints,
    trigger_preemption,
)
from deliverytwin.backlog import latest_backlog
from deliverytwin.storage.connection import connect
from deliverytwin.intake import approve_baseline
from deliverytwin.llm import LLMError
from deliverytwin.portfolio import custom_plan, option_confidence, planned_loss_hours
from deliverytwin.recovery import (
    _portfolio_base,
    capacity_confidence,
    instruction_constraints,
    predict_scenario,
    tune_scenario,
)

ANSWERS = {"architecture": "incremental", "data": "event_sync", "security": "pii_consent", "reuse": "reuse_mdm"}
REQUEST = "We need one view of every customer across the business."
ACTIONS = {"milestone": "Rebaseline.", "azure_devops": "Defer items.", "servicenow": "Escalate.", "workforce": "Shield the lead."}


class TunerLLM:
    """Answers each tuning round with the next scripted lever set and records what it was told."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.messages = []

    def __call__(self, messages, json_mode=False):
        self.messages.append(messages)
        if not self.rounds:
            raise LLMError("No more scripted rounds.")
        levers = self.rounds.pop(0)
        return json.dumps({**levers, "feasible": True, "rationale": "Move the date before adding people.",
                           "trade_offs": ["The date moves."], "risks": ["Stakeholders may resist."],
                           "actions": ACTIONS, "predicted_on_time_pct": 99})


class ScenarioTuningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        initialize_database(self.connection)
        self.today = date.today()
        seed_demo(self.connection, self.today)
        approve_baseline(self.connection, REQUEST, ANSWERS, self.today, adjustments={"allocated_fte": 7.0})
        simulate_sprints(self.connection, 3, self.today, scenario="collision")
        self.report = build_workflow_report(self.connection)
        self.start = {
            "allocated_fte": self.report["signals"]["allocated_fte"], "target_date": self.report["initiative"]["target_end"],
            "scope_defer_pct": 0, "incremental_cost_k": 0,
        }

    def tune(self, llm, **payload):
        configured = patch("deliverytwin.recovery.openai_configured", return_value=llm is not None)
        complete = patch("deliverytwin.recovery.complete", side_effect=llm)
        with configured, complete:
            return tune_scenario(self.report, {**self.start, **payload}, [])

    def test_the_ai_proposes_levers_code_scores_them_and_the_score_is_fed_back(self) -> None:
        target_end = date.fromisoformat(self.start["target_date"])
        llm = TunerLLM([
            {**self.start, "target_date": (target_end + timedelta(weeks=1)).isoformat()},
            {**self.start, "allocated_fte": 8.5, "target_date": (target_end + timedelta(weeks=40)).isoformat()},
        ])
        result = self.tune(llm, instruction="Keep the budget at zero and get us above 70%.")
        tuning = result["tuning"]
        self.assertEqual(result["prediction"]["source"], "llm")
        self.assertEqual((tuning["rounds"], tuning["reached"], tuning["mode"]), (2, True, "what_if"))
        # The percentage is computed by code from the final levers; the AI's own 99% is ignored.
        expected = capacity_confidence(self.report, 8.5, target_end + timedelta(weeks=40), 0)
        self.assertEqual(result["confidence_pct"], expected)
        self.assertGreaterEqual(expected, 70)
        self.assertNotEqual(result["confidence_pct"], 99)
        feedback = json.loads(llm.messages[1][-1]["content"])["feedback"]
        self.assertIn("below the 70% target", feedback)
        self.assertIn("Keep the budget at zero", llm.messages[0][1]["content"])
        self.assertNotIn("Keep the budget at zero", llm.messages[0][0]["content"])
        self.assertTrue(any("Target date" in change for change in tuning["changes"]))

    def test_out_of_bounds_levers_are_rejected_and_the_reason_is_fed_back(self) -> None:
        later = (date.fromisoformat(self.start["target_date"]) + timedelta(weeks=60)).isoformat()
        llm = TunerLLM([{**self.start, "allocated_fte": 99}, {**self.start, "target_date": later}])
        result = self.tune(llm, instruction="Whatever it takes.")
        self.assertIn("rejected", json.loads(llm.messages[1][-1]["content"])["feedback"])
        self.assertEqual(result["allocated_fte"], self.start["allocated_fte"])
        self.assertEqual(result["target_date"], later)

    def test_without_an_ai_a_search_honors_the_constraints_in_the_instruction(self) -> None:
        result = self.tune(None, instruction="No extra budget and keep the target date.", incremental_cost_k=25)
        tuning = result["tuning"]
        self.assertEqual(result["prediction"]["source"], "rules")
        self.assertEqual(result["prediction"]["fallback_reason"]["code"], "not_configured")
        self.assertEqual(result["target_date"], self.start["target_date"])
        self.assertEqual(result["incremental_cost_k"], 0)
        self.assertEqual(tuning["constraints"], ["no contractor or budget spend", "keep the target date"])
        self.assertIn("honoring: no contractor or budget spend, keep the target date", result["prediction"]["commentary"])
        free = self.tune(None, instruction="Get it above 70%.")
        self.assertTrue(free["tuning"]["reached"])
        self.assertGreaterEqual(free["confidence_pct"], 70)

    def test_an_unusable_ai_falls_back_to_the_search(self) -> None:
        def broken(messages, json_mode=False):
            raise LLMError("OpenAI request failed with HTTP 500.")

        result = self.tune(broken, instruction="Get it above 70%.")
        self.assertEqual(result["prediction"]["source"], "rules")
        self.assertEqual(result["prediction"]["fallback_reason"]["code"], "request_failed")
        self.assertTrue(result["tuning"]["reached"])

    def test_requests_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.tune(None, instruction="   ")
        with self.assertRaises(ValueError):
            self.tune(None, instruction="Get it above 70%.", target_pct=101)
        with self.assertRaises(ValueError):
            self.tune(None, instruction="Get it above 70%.", portfolio_option="hybrid_replan")

    def test_instruction_constraints_are_recognized(self) -> None:
        self.assertEqual(instruction_constraints("Don't defer scope and avoid contractors"), ["keep_budget", "keep_scope"])
        self.assertEqual(instruction_constraints("without moving the date or borrowing from PRJ-024"),
                         ["keep_date", "no_borrow"])
        self.assertEqual(instruction_constraints("no extra people"), ["keep_fte"])
        self.assertEqual(instruction_constraints("make it better"), [])


class PortfolioTuningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        self.addCleanup(self.connection.close)
        initialize_database(self.connection)
        today = date.today()
        seed_demo(self.connection, today)
        approve_baseline(self.connection, REQUEST, ANSWERS, today, adjustments={"allocated_fte": 8.5})
        simulate_sprints(self.connection, 2, today, scenario="healthy")
        trigger_preemption(self.connection)
        self.report = build_workflow_report(self.connection)
        self.report["portfolio"]["backlog"] = latest_backlog(self.connection)

    def tune(self, instruction, llm=None):
        with patch("deliverytwin.recovery.openai_configured", return_value=llm is not None), \
                patch("deliverytwin.recovery.complete", side_effect=llm):
            return tune_scenario(self.report, {"instruction": instruction, "portfolio_option": "hybrid_replan"}, [])

    def test_portfolio_levers_are_tuned_and_scored_by_the_portfolio_model(self) -> None:
        result = self.tune("No extra budget.")
        levers = result["tuning"]["levers"]
        self.assertEqual(result["tuning"]["mode"], "portfolio")
        self.assertEqual(levers["backfill_fte"], 0)
        self.assertEqual(levers["target_date"], self.report["initiative"]["target_end"])
        self.assertEqual(result["cost_impact"]["label"], "$0")
        expected = option_confidence(_portfolio_base(self.report), custom_plan(levers, planned_loss_hours()))
        self.assertEqual(result["confidence_pct"], expected)
        self.assertGreaterEqual(expected, 70)
        self.assertEqual((result["portfolio_option"], result["portfolio_levers"]), ("custom", levers))
        self.assertIn("pi_plan", result)

    def test_when_the_portfolio_levers_are_ruled_out_the_date_closes_the_gap(self) -> None:
        result = self.tune("No budget, no borrowing and don't defer scope.")
        levers = result["tuning"]["levers"]
        self.assertTrue(result["tuning"]["reached"])
        self.assertEqual((levers["deferred_hours"], levers["borrowed_fte"], levers["backfill_fte"]), (0, 0, 0))
        self.assertGreater(levers["target_date"], self.report["initiative"]["target_end"])
        self.assertTrue(any("target date moves" in text for text in result["prediction"]["trade_offs"]))

    def test_an_unreachable_target_is_reported_rather_than_faked(self) -> None:
        result = self.tune("No budget, no borrowing, don't defer scope and keep the target date.")
        self.assertFalse(result["tuning"]["reached"])
        self.assertLess(result["confidence_pct"], 70)
        self.assertIn("No mix within those limits reaches 70%", result["prediction"]["commentary"])

    def test_the_ai_tunes_portfolio_levers_with_computed_trade_offs(self) -> None:
        llm = TunerLLM([{"deferred_hours": 0, "borrowed_fte": 2.0, "backfill_fte": 1.0}])
        result = self.tune("Protect all scope.", llm)
        self.assertEqual(result["prediction"]["source"], "llm")
        self.assertEqual(result["tuning"]["levers"], {
            "deferred_hours": 0, "borrowed_fte": 2.0, "backfill_fte": 1.0,
            "allocated_fte": 8.5, "target_date": self.report["initiative"]["target_end"],
        })
        self.assertTrue(any("PRJ-024" in text for text in result["prediction"]["trade_offs"]))

    def test_a_tuned_portfolio_plan_can_be_previewed_and_applied(self) -> None:
        levers = self.tune("No extra budget.")["tuning"]["levers"]
        with patch("deliverytwin.recovery.openai_configured", return_value=False):
            preview = predict_scenario(self.report, {"portfolio_option": "custom", "portfolio_levers": levers}, [])
        self.assertEqual(preview["recommended_option"], "custom")
        save_recovery_scenario(self.connection, preview)
        self.assertEqual(_latest_recovery_scenario(self.connection)["recommended_option"], "custom")
        with self.assertRaises(ValueError):
            predict_scenario(self.report, {"portfolio_option": "custom", "portfolio_levers": {"borrowed_fte": 3}}, [])


if __name__ == "__main__":
    unittest.main()
