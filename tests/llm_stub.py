"""Scripted stand-in for the OpenAI client shared by the prediction and UI tests."""
import json

from deliverytwin.llm import LLMError


def llm_questions():
    return {"questions": [
        {"id": "cutover", "category": "Delivery", "question": "How will consumers move over?",
         "options": [
             {"value": "phased", "label": "Move consumers in phases",
              "effects": {"structure": "legacy-modernization", "delivery_pattern": "incremental-integration",
                          "systems": ["crm", "service-management"], "points": 0}},
             {"value": "big_bang", "label": "One coordinated cutover",
              "effects": {"delivery_pattern": "big-bang-cutover", "points": 1}}],
         "suggested": "phased", "rationale": "Most similar projects phased the move, like HIST-001 did.",
         "evidence_project_ids": ["HIST-001", "NOT-A-PROJECT"]},
        {"id": "sync", "category": "Data", "question": "How fresh must profiles be?",
         "options": [
             {"value": "events", "label": "Near real time",
              "effects": {"systems": ["event-streaming", "commerce"], "roles": ["data-engineer"], "points": 2}},
             {"value": "batch", "label": "Scheduled batch",
              "effects": {"systems": ["batch-integration"], "points": 1}}],
         "suggested": "events", "rationale": "Customer teams expect fresh data.", "evidence_project_ids": []},
        {"id": "privacy", "category": "Privacy", "question": "Which privacy controls apply?",
         "options": [
             {"value": "full", "label": "PII, consent, and retention controls",
              "effects": {"roles": ["privacy-engineer"], "dependencies": ["privacy-review", "consent-management"],
                          "points": 2}},
             {"value": "light", "label": "Standard access review", "effects": {"points": 0}}],
         "suggested": "full", "rationale": "Customer data is personal.", "evidence_project_ids": []},
        {"id": "identity", "category": "Reuse", "question": "What can be reused?",
         "options": [
             {"value": "reuse", "label": "Existing match rules and profile APIs",
              "effects": {"systems": ["mdm", "identity-resolution", "profile-api"],
                          "dependencies": ["crm-mdm-integration"]}},
             {"value": "build", "label": "Build new identity services",
              "effects": {"roles": ["platform-engineer"], "systems": ["not-a-real-system"], "points": 1}}],
         "suggested": "reuse", "rationale": "MDM already exists.", "evidence_project_ids": []},
    ]}


class ScriptedLLM:
    """Stands in for the OpenAI client; each call type can succeed or fail independently."""

    def __init__(self, questions=None, forecast="valid", fail=()):
        self.fail = set(fail)
        self.prompts = []
        self.questions = questions if questions is not None else llm_questions()
        self.forecast = forecast
        self.calls = []

    def __call__(self, messages, json_mode=False):
        system = messages[0]["content"]
        payload = json.loads(messages[1]["content"])
        self.prompts.append(messages[1]["content"])
        if "clarifying questions" in system:
            self.calls.append("questions")
            if "questions" in self.fail:
                raise LLMError("OpenAI request failed with HTTP 401.")
            return json.dumps(self.questions)
        if "delivery forecasting expert" in system:
            self.calls.append("forecast")
            if "forecast" in self.fail:
                raise LLMError("OpenAI request failed with HTTP 429.")
            if self.forecast == "invalid":
                return json.dumps({"p50_effort_hours": 10})
            first = payload["similar_historical_projects"][0]["project_name"]
            return json.dumps({
                "p50_effort_hours": 4900, "p80_effort_hours": 6100, "p90_effort_hours": 6900,
                "on_time_probability": 0.64, "confidence": 0.77,
                "key_risks": ["Identity matching rules are shared"], "recommendations": ["Phase scope"],
                "historical_projects_used": [first],
            })
        self.calls.append("rerank")
        raise LLMError("rerank not scripted")
