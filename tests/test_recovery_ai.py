import unittest
from datetime import date
from unittest.mock import patch

from deliverytwin import connect, initialize_database, seed_demo
from deliverytwin.app import simulate_sprints
from deliverytwin.recovery import analyze_intervention, predict_scenario, validate_prediction
from deliverytwin import build_workflow_report

try:
    from tests.llm_stub import ScriptedLLM
except ImportError:  # discovery with -s tests
    from llm_stub import ScriptedLLM

START = date(2026, 10, 17)


class RecoveryAiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, START)
        simulate_sprints(self.connection, 3, START)
        self.report = build_workflow_report(self.connection)
        self.late = [{"project_name": "Late One", "planned_effort_hours": 3000, "actual_effort_hours": 4000,
                      "planned_duration_weeks": 20, "actual_duration_weeks": 28, "outcome": "late",
                      "lessons_learned": "Blockers were escalated too late."}]

    def tearDown(self) -> None:
        self.connection.close()

    def with_llm(self, llm):
        return (patch("deliverytwin.recovery.openai_configured", return_value=True),
                patch("deliverytwin.recovery.complete", side_effect=llm))

    def test_ai_writes_diagnosis_and_concrete_validated_scenarios(self) -> None:
        llm = ScriptedLLM()
        configured, complete = self.with_llm(llm)
        with configured, complete:
            analysis = analyze_intervention(self.report, self.late)
        self.assertEqual((analysis["source"], analysis["fallback_reason"]), ("llm", None))
        self.assertEqual(analysis["recommended"], "phase_scope")
        self.assertEqual([s["id"] for s in analysis["scenarios"]], ["add_contractors", "phase_scope", "extend_date"])
        phase = analysis["scenarios"][1]
        self.assertEqual((phase["predicted_on_time_pct"], phase["predicted_slip_days"], phase["scope_defer_pct"]), (78, 5, 40))
        self.assertIsInstance(phase["model_check_pct"], int)
        self.assertEqual(set(phase["actions"]), {"milestone", "azure_devops", "servicenow", "workforce"})
        self.assertIn("Late One", llm.prompts[0])
        self.assertEqual(llm.calls, ["recovery_analysis"])

    def test_unusable_ai_output_falls_back_to_labeled_rules_scenarios(self) -> None:
        cases = {
            "invalid": ScriptedLLM(recovery="invalid"),
            "failed": ScriptedLLM(fail=("recovery",)),
        }
        codes = {}
        for name, llm in cases.items():
            configured, complete = self.with_llm(llm)
            with configured, complete:
                analysis = analyze_intervention(self.report, self.late)
            self.assertEqual(analysis["source"], "rules")
            self.assertEqual([s["id"] for s in analysis["scenarios"]], ["contractors", "squad_reallocation", "phase_and_unblock"])
            self.assertIn(analysis["recommended"], {s["id"] for s in analysis["scenarios"]})
            codes[name] = analysis["fallback_reason"]
        self.assertEqual(codes["invalid"]["code"], "invalid_response")
        self.assertEqual(codes["failed"]["code"], "request_failed")
        self.assertIn("HTTP 503", codes["failed"]["detail"])
        with patch("deliverytwin.recovery.openai_configured", return_value=False):
            self.assertEqual(analyze_intervention(self.report, self.late)["fallback_reason"], {"code": "not_configured"})

    def test_what_if_prediction_is_ai_driven_with_a_rules_cross_check(self) -> None:
        llm = ScriptedLLM()
        configured, complete = self.with_llm(llm)
        payload = {"allocated_fte": 7.5, "target_date": self.report["initiative"]["target_end"],
                   "scope_defer_pct": 20, "incremental_cost_k": 50}
        with configured, complete:
            result = predict_scenario(self.report, payload, self.late)
        self.assertEqual((result["confidence_pct"], result["projected_slip_days"]), (66, 11))
        self.assertEqual(result["prediction"]["source"], "llm")
        self.assertIsInstance(result["model_check_pct"], int)
        self.assertNotEqual(result["model_check_pct"], 66)
        updates = {u["source_key"]: u["payload"] for u in result["target_updates"]}
        self.assertIn("CAB blocker", updates["servicenow"]["change_request_action"])
        self.assertIn("production support", updates["ukg_wfm"]["workforce_action"])
        self.assertFalse(result["writeback_performed"])

        echoed = {**payload, "prediction": {**result["prediction"], "predicted_on_time_pct": 71}}
        before = len(llm.calls)
        with configured, complete:
            reused = predict_scenario(self.report, echoed, self.late)
        self.assertEqual(len(llm.calls), before)
        self.assertEqual(reused["confidence_pct"], 71)
        with self.assertRaises(ValueError):
            predict_scenario(self.report, {**payload, "prediction": {"predicted_on_time_pct": 500}}, self.late)
        with self.assertRaises(ValueError):
            predict_scenario(self.report, {**payload, "allocated_fte": 99}, self.late)

    def test_what_if_without_a_usable_prediction_uses_the_capacity_model(self) -> None:
        payload = {"allocated_fte": 7.5}
        for llm in (ScriptedLLM(recovery="invalid"), ScriptedLLM(fail=("recovery",))):
            configured, complete = self.with_llm(llm)
            with configured, complete:
                result = predict_scenario(self.report, payload, self.late)
            self.assertEqual(result["prediction"]["source"], "rules")
            self.assertEqual(result["confidence_pct"], result["model_check_pct"])
            self.assertIsNotNone(result["prediction"]["fallback_reason"])

    def test_prediction_validation_rejects_out_of_range_or_incomplete_output(self) -> None:
        good = {"predicted_on_time_pct": 70, "predicted_slip_days": 3, "commentary": "ok", "trade_offs": [], "risks": [],
                "actions": {k: "x" for k in ("milestone", "azure_devops", "servicenow", "workforce")}}
        self.assertEqual(validate_prediction(good)["predicted_on_time_pct"], 70)
        for bad in ({**good, "predicted_on_time_pct": 101}, {**good, "predicted_slip_days": -1},
                    {**good, "commentary": ""}, {**good, "actions": {"milestone": "x"}},
                    {**good, "risks": "no"}, "text", None):
            self.assertIsNone(validate_prediction(bad))


if __name__ == "__main__":
    unittest.main()
