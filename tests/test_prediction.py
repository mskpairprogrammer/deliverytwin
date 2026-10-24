import json
import unittest
from datetime import date
from unittest.mock import patch

from deliverytwin import connect, initialize_database, seed_demo
from deliverytwin.intake import (
    IntakeError,
    _validate_basis,
    analyze_intake,
    approve_baseline,
    estimate_intake,
    evaluate_intake,
)
try:
    from tests.llm_stub import ScriptedLLM, llm_questions
except ImportError:  # discovery with -s tests
    from llm_stub import ScriptedLLM, llm_questions

REQUEST = "We need one view of every customer across the business. Teams get different answers."
DAY = date(2026, 10, 17)


class PredictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect(":memory:")
        initialize_database(self.connection)
        seed_demo(self.connection, DAY)

    def tearDown(self) -> None:
        self.connection.close()

    def answers(self, analysis):
        return {item["id"]: item["suggested"] for item in analysis["clarifications"]}

    def run_with(self, llm):
        return (patch("deliverytwin.intake.openai_configured", return_value=True),
                patch("deliverytwin.intake.complete", side_effect=llm))

    def test_llm_generates_tailored_validated_questions_with_rationale(self) -> None:
        llm = ScriptedLLM()
        configured, complete = self.run_with(llm)
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
        self.assertEqual(analysis["question_source"]["type"], "llm")
        self.assertEqual([c["id"] for c in analysis["clarifications"]], ["cutover", "sync", "privacy", "identity"])
        first = analysis["clarifications"][0]
        self.assertEqual(first["suggested"], "phased")
        self.assertIn("phased", first["rationale"])
        self.assertEqual(first["category"], "Delivery")
        self.assertEqual([e["project"] for e in first["evidence"]], ["Customer Profile Unification"])
        build = analysis["clarifications"][3]["options"][1]["effects"]
        self.assertEqual(build["roles"], ["platform-engineer"])
        self.assertNotIn("systems", build)
        self.assertEqual(llm.calls, ["questions"])

    def test_invalid_llm_questions_fall_back_to_the_template_set(self) -> None:
        broken = llm_questions()
        broken["questions"][0]["suggested"] = "missing"
        for bad in (broken, {"questions": llm_questions()["questions"][:3]}, {"nope": 1}):
            configured, complete = self.run_with(ScriptedLLM(questions=bad))
            with configured, complete:
                analysis = analyze_intake(self.connection, REQUEST)
            self.assertEqual(analysis["question_source"]["type"], "template")
            self.assertEqual([c["id"] for c in analysis["clarifications"]],
                             ["architecture", "data", "security", "reuse"])

    def test_kebab_case_ids_from_the_model_are_spelled_in_snake_case(self) -> None:
        # Models copy the hyphenated vocabulary (big-bang-cutover) into ids; that alone must not discard the set.
        kebab = llm_questions()
        first = kebab["questions"][0]
        first["id"], first["options"][1]["value"], first["suggested"] = "cut-over", "big-bang", "Big-Bang"
        configured, complete = self.run_with(ScriptedLLM(questions=kebab))
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
        self.assertEqual(analysis["question_source"]["type"], "llm")
        question = analysis["clarifications"][0]
        self.assertEqual((question["id"], question["suggested"]), ("cut_over", "big_bang"))
        self.assertEqual([option["value"] for option in question["options"]], ["phased", "big_bang"])

    def test_fallbacks_say_why_the_ai_was_not_used(self) -> None:
        def source(patches, llm=None):
            with patches[0], patches[1]:
                return analyze_intake(self.connection, REQUEST)["question_source"]

        with patch("deliverytwin.intake.openai_configured", return_value=False):
            self.assertEqual(analyze_intake(self.connection, REQUEST)["question_source"]["reason"],
                             {"code": "not_configured"})
        failing = source(self.run_with(ScriptedLLM(fail=("questions",))))
        self.assertEqual(failing["type"], "template")
        self.assertEqual(failing["reason"]["code"], "request_failed")
        self.assertIn("HTTP 401", failing["reason"]["detail"])
        broken = llm_questions()
        broken["questions"][0]["suggested"] = "missing"
        self.assertEqual(source(self.run_with(ScriptedLLM(questions=broken)))["reason"]["code"], "invalid_response")

    def test_forecast_fallbacks_carry_their_reason(self) -> None:
        reasons = {}
        for name, llm in (("failed", ScriptedLLM(fail=("forecast",))), ("invalid", ScriptedLLM(forecast="invalid"))):
            configured, complete = self.run_with(llm)
            with configured, complete:
                analysis = analyze_intake(self.connection, REQUEST)
                reasons[name] = estimate_intake(
                    self.connection, REQUEST, self.answers(analysis), DAY, questions=analysis["clarifications"]
                )["prediction"]["fallback_reason"]
        self.assertEqual(reasons["failed"]["code"], "request_failed")
        self.assertIn("HTTP 429", reasons["failed"]["detail"])
        self.assertEqual(reasons["invalid"]["code"], "invalid_response")
        configured, complete = self.run_with(ScriptedLLM())
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
            ok = estimate_intake(self.connection, REQUEST, self.answers(analysis), DAY, questions=analysis["clarifications"])
        self.assertIsNone(ok["prediction"]["fallback_reason"])

    def test_generated_answers_drive_the_fingerprint_and_tampering_is_rejected(self) -> None:
        configured, complete = self.run_with(ScriptedLLM())
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
        questions, answers = analysis["clarifications"], self.answers(analysis)
        result = evaluate_intake(self.connection, REQUEST, answers, DAY, questions=questions)
        fingerprint = result["fingerprint"]
        self.assertIn("event-streaming", fingerprint["systems"])
        self.assertIn("privacy-engineer", fingerprint["roles"])
        self.assertIn("consent-management", fingerprint["dependencies"])
        self.assertEqual(fingerprint["complexity"], "high")
        self.assertEqual(fingerprint["delivery_pattern"], "incremental-integration")
        batch = evaluate_intake(self.connection, REQUEST, {**answers, "sync": "batch"}, DAY, questions=questions)
        self.assertIn("batch-integration", batch["fingerprint"]["systems"])
        for bad in ([{**questions[0], "id": "Bad Id"}] + questions[1:], questions[:2], "x"):
            with self.assertRaises(IntakeError):
                evaluate_intake(self.connection, REQUEST, answers, DAY, questions=bad)
        with self.assertRaises(IntakeError):
            evaluate_intake(self.connection, REQUEST, {**answers, "sync": "missing"}, DAY, questions=questions)

    def test_llm_prediction_becomes_the_commit_baseline_with_a_deterministic_comparison(self) -> None:
        llm = ScriptedLLM()
        configured, complete = self.run_with(llm)
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
            estimate = estimate_intake(
                self.connection, REQUEST, self.answers(analysis), DAY, questions=analysis["clarifications"])
        prediction = estimate["prediction"]
        self.assertEqual(prediction["source"], "llm")
        self.assertEqual(prediction["forecast"]["p50_effort_hours"], 4900)
        commitment = estimate["commitment"]
        self.assertEqual((commitment["p50_hours"], commitment["p80_hours"]), (4900, 6100))
        self.assertEqual(estimate["basis"], {"source": "llm", "p50_hours": 4900, "p80_hours": 6100, "p90_hours": 6900})
        self.assertEqual(commitment["basis"], estimate["basis"])
        self.assertNotEqual(estimate["deterministic_rom"]["p50_hours"], 4900)
        self.assertTrue(0 < commitment["fit_probability_pct"] < 100)
        self.assertEqual(llm.calls.count("forecast"), 1)

    def test_what_ifs_reuse_the_basis_and_move_confidence_analytically(self) -> None:
        configured, complete = self.run_with(ScriptedLLM())
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
            answers, questions = self.answers(analysis), analysis["clarifications"]
            estimate = estimate_intake(self.connection, REQUEST, answers, DAY, questions=questions)
        basis = estimate["basis"]

        def commitment(**adjustments):
            return evaluate_intake(
                self.connection, REQUEST, answers, DAY, adjustments=adjustments, questions=questions, basis=basis
            )["commitment"]

        base, fewer, scoped = commitment(), commitment(allocated_fte=5.0), commitment(scope_change_pct=20)
        self.assertEqual(base["p80_hours"], 6100)
        self.assertLess(fewer["fit_probability_pct"], base["fit_probability_pct"])
        self.assertLess(scoped["fit_probability_pct"], base["fit_probability_pct"])
        self.assertGreater(scoped["p80_hours"], base["p80_hours"])

    def test_invalid_llm_forecast_falls_back_to_a_labeled_statistical_basis(self) -> None:
        configured, complete = self.run_with(ScriptedLLM(forecast="invalid"))
        with configured, complete:
            analysis = analyze_intake(self.connection, REQUEST)
            estimate = estimate_intake(
                self.connection, REQUEST, self.answers(analysis), DAY, questions=analysis["clarifications"])
        self.assertEqual(estimate["prediction"]["source"], "statistical")
        self.assertEqual(estimate["basis"]["source"], "statistical")
        self.assertEqual(estimate["commitment"]["p50_hours"], estimate["basis"]["p50_hours"])

    def test_without_an_llm_the_whole_flow_still_works(self) -> None:
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            analysis = analyze_intake(self.connection, REQUEST)
            estimate = estimate_intake(
                self.connection, REQUEST, self.answers(analysis), DAY, questions=analysis["clarifications"])
        self.assertEqual(analysis["question_source"]["type"], "template")
        self.assertEqual(estimate["prediction"]["source"], "statistical")
        self.assertIn("p50_hours", estimate["deterministic_rom"])

    def test_basis_is_validated_and_approval_records_it(self) -> None:
        good = {"source": "llm", "p50_hours": 4900, "p80_hours": 6100, "p90_hours": 6900}
        self.assertEqual(_validate_basis(self.connection, good)["p80_hours"], 6100)
        self.assertIsNone(_validate_basis(self.connection, None))
        for bad in (
            {**good, "source": "human"}, {**good, "p50_hours": 7000}, {**good, "p90_hours": 5000},
            {**good, "p50_hours": 10}, {**good, "p90_hours": 10_000_000}, {**good, "p80_hours": "x"}, [1],
        ):
            with self.assertRaises(IntakeError):
                _validate_basis(self.connection, bad)
        with patch("deliverytwin.intake.openai_configured", return_value=False):
            analysis = analyze_intake(self.connection, REQUEST)
        approved = approve_baseline(
            self.connection, REQUEST, self.answers(analysis), DAY,
            adjustments={"allocated_fte": 8.0}, questions=analysis["clarifications"], basis=good,
        )
        self.assertEqual((approved["baseline"]["p50_hours"], approved["baseline"]["p80_hours"]), (4900, 6100))
        stored = self.connection.execute("SELECT answers_json FROM dt_commitment_baselines").fetchone()[0]
        self.assertEqual(json.loads(stored)["basis"]["source"], "llm")


if __name__ == "__main__":
    unittest.main()
