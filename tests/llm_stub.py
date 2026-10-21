"""Scripted stand-in for the OpenAI client shared by the prediction and UI tests."""
import json
from datetime import date, timedelta
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

    def __init__(self, questions=None, forecast="valid", fail=(), recovery="valid", challenge="valid", brief="valid"):
        self.recovery = recovery
        self.challenge = challenge
        self.brief = brief
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
        if "recovery advisor" in system:
            return self._recovery(system, payload)
        if "governance reviewer" in system:
            return self._challenge(payload)
        if "what-if question" in system:
            return self._ask(payload)
        if "delivery tracking analyst" in system:
            return self._brief(payload)
        self.calls.append("rerank")
        raise LLMError("rerank not scripted")

    def _challenge(self, payload):
        self.calls.append("challenge")
        if "challenge" in self.fail:
            raise LLMError("OpenAI request failed with HTTP 502.")
        severity = "catastrophic" if self.challenge == "invalid" else "high"
        verdict = "ready" if self.challenge == "ready" else "ready_with_conditions"
        return json.dumps({
            "verdict": verdict, "summary": "The date is tight and rests on one shared identity-matching service.",
            "risks": [
                {"title": "Shared identity matching", "severity": severity,
                 "detail": "Two similar projects slipped waiting on this service.", "evidence": "HIST-001 slipped 6 weeks"},
                {"title": "Thin capacity headroom", "severity": "medium",
                 "detail": "Required FTE is close to allocated.", "evidence": "Required vs allocated FTE"},
            ],
            "conditions": ["Get a dated CAB slot for identity matching.", "Name a backup engineer."],
        })

    def _ask(self, payload):
        self.calls.append("ask")
        if "ask" in self.fail:
            raise LLMError("OpenAI request failed with HTTP 503.")
        question, current = payload["question"].lower(), payload["current_levers"]
        levers = {
            "answerable": True, "reading": "Reads as a change to the commitment levers.",
            "allocated_fte": current["allocated_fte"], "target_date": current["target_date"],
            "scope_change_pct": current["scope_change_pct"], "caveats": ["Assumes no replacement is found."],
        }
        if "lose" in question:
            levers["allocated_fte"] = max(0.5, current["allocated_fte"] - 1)
            levers["reading"] = "Reads as losing one engineer, so allocated FTE drops by 1."
        elif "delay" in question:
            moved = date.fromisoformat(current["target_date"]) + timedelta(days=28)
            levers.update(target_date=moved.isoformat(), reading="Reads as moving the date out four weeks.")
        elif "scope" in question:
            levers.update(scope_change_pct=float(current["scope_change_pct"] + 20),
                          reading="Reads as 20% more scope.")
        elif "huge" in question:
            levers["allocated_fte"] = 99
        elif "nothing" not in question:
            return json.dumps({"answerable": False, "reason": "That cannot be expressed as capacity, date, or scope."})
        return json.dumps(levers)

    def _brief(self, payload):
        self.calls.append("brief")
        if "brief" in self.fail:
            raise LLMError("OpenAI request failed with HTTP 500.")
        drivers = payload["variance"]["drivers"]
        shown = drivers[:-1] if self.brief == "invalid" else drivers
        return json.dumps({
            "headline": "Support load and a stuck change request cut confidence.",
            "narrative": "Production support and a blocked change request are the main reasons the forecast moved.",
            "drivers": [{"key": item["key"], "explanation": f"Evidence from {item['source_name']}."} for item in shown],
            "watch_next": ["CAB decision on identity matching", "Lead engineer support hours"],
        })

    def _recovery(self, system, payload):
        analysis = "propose 3 distinct recovery scenarios" in system
        self.calls.append("recovery_analysis" if analysis else "recovery_prediction")
        if "recovery" in self.fail:
            raise LLMError("OpenAI request failed with HTTP 503.")
        actions = {
            "milestone": "Rebaseline the milestone with the AI-predicted slip.",
            "azure_devops": "Defer the lowest-value unplanned stories.",
            "servicenow": "Escalate the identity-matching CAB blocker to its sponsor.",
            "workforce": "Move one engineer onto production support.",
        }
        if not analysis:
            if self.recovery == "invalid":
                return json.dumps({"predicted_on_time_pct": 400})
            return json.dumps({
                "predicted_on_time_pct": 66, "predicted_slip_days": 11,
                "commentary": "Extra capacity helps but the CAB blocker still gates the work.",
                "trade_offs": ["Higher burn rate"], "risks": ["Onboarding delay"], "actions": actions,
            })
        context = payload
        alloc, cap = context["signals"]["allocated_fte"], context["signals"]["capacity_fte"]
        target = context["initiative"]["current_target_date"]
        year, month, day = (int(part) for part in target.split("-"))
        later = f"{year + 1}-{month:02d}-{min(day, 28):02d}"

        def scenario(identifier, fte, date, scope, cost, pct, slip):
            return {
                "id": identifier, "title": identifier.replace("_", " ").title(),
                "summary": f"Scenario {identifier}.", "allocated_fte": fte, "target_date": date,
                "scope_defer_pct": scope, "incremental_cost_k": cost, "predicted_on_time_pct": pct,
                "predicted_slip_days": slip, "commentary": f"Because of {identifier}.",
                "trade_offs": ["Trade-off"], "risks": ["Risk"], "actions": actions,
            }

        scenarios = [
            scenario("add_contractors", min(cap, alloc + 1.5), target, 0, 118, 71, 9),
            scenario("phase_scope", alloc, target, 40, 0, 78, 5),
            scenario("extend_date", alloc, later, 0, 0, 83, 0),
        ]
        if self.recovery == "invalid":
            scenarios[1]["allocated_fte"] = cap + 10  # the recommended scenario is out of bounds
        if self.recovery == "one_bad":
            scenarios[0]["allocated_fte"] = cap + 10
        return json.dumps({
            "diagnosis": "Capacity is draining into production support while scope grows.",
            "recommended": "phase_scope", "scenarios": scenarios,
        })
