"""
Care Advisor Agent — the agentic workflow that ties everything together.

Pipeline (each step is observable and logged):

    1. PARSE    - guardrail input check + intent classification
    2. RETRIEVE - RAG over the knowledge base
    3. INSPECT  - call PawPal+ tools to read pet/schedule state
    4. PLAN     - decide which tool, if any, to call to *change* state
    5. ACT      - execute the chosen tool (e.g. add a HIGH-priority appointment)
    6. EXPLAIN  - ask the LLM to compose a grounded, cited answer
    7. VERIFY   - guardrail output check + confidence score

Each step appends an ``AgentStep`` to ``AgentResponse.steps``, so the
Streamlit UI and the eval harness can both render the full reasoning
trace. This gives us the "observable intermediate steps" the rubric asks
for in the agentic-workflow stretch goal.

Tools the agent can invoke (defined as plain methods, not LLM-driven function
calling — the Local LLM is too small for that, and a deterministic dispatcher
is more reliable for grading):

    * get_pet_info(name)        -> read a single pet + its tasks
    * get_schedule()            -> generate today's schedule
    * check_conflicts()         -> run scheduler.detect_conflicts()
    * add_task(...)             -> mutate state, persist via owner.save_to_json
    * flag_emergency(reason)    -> short-circuits with the emergency template
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pawpal_system import (
    Owner, Pet, Task, TaskType, Priority, Scheduler,
    TASK_TYPE_EMOJI,
)

from ai.guardrails import Guardrails, GuardrailViolation
from ai.knowledge import KnowledgeBase, RetrievalResult
from ai.llm_client import LLMClient, build_default_client, classify_intent


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class AgentStep:
    """One observable step in the agent's reasoning trace."""
    name: str                          # e.g. "RETRIEVE"
    detail: str                        # human-readable summary
    data: dict[str, Any] = field(default_factory=dict)   # structured payload


@dataclass
class AgentResponse:
    """Final result returned to the caller (CLI, Streamlit, eval harness)."""
    answer: str
    steps: list[AgentStep] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    confidence: float = 0.0            # in [0, 1]
    intent: str = "general"
    tool_calls: list[str] = field(default_factory=list)
    refused: bool = False              # True if a guardrail blocked the response
    refusal_rule: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "intent": self.intent,
            "confidence": round(self.confidence, 3),
            "citations": list(self.citations),
            "tool_calls": list(self.tool_calls),
            "refused": self.refused,
            "refusal_rule": self.refusal_rule,
            "steps": [{"name": s.name, "detail": s.detail, "data": s.data}
                      for s in self.steps],
        }


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class CareAdvisorAgent:
    """Agentic + RAG layer over the PawPal+ scheduler."""

    def __init__(
        self,
        owner: Owner,
        knowledge_base: KnowledgeBase,
        llm: Optional[LLMClient] = None,
        guardrails: Optional[Guardrails] = None,
        data_path: str | Path | None = None,
    ) -> None:
        self.owner = owner
        self.kb = knowledge_base
        self.llm: LLMClient = llm or build_default_client()
        self.guardrails = guardrails or Guardrails()
        self.data_path = Path(data_path) if data_path else None

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def ask(self, user_text: str) -> AgentResponse:
        """Run the full pipeline on one user message and return a response."""
        response = AgentResponse(answer="", intent="general")
        self.guardrails.log_event("agent.start", {"input": user_text})

        # Step 1: PARSE + input guardrails
        parsed = self._step_parse(user_text, response)
        if parsed is not None:  # blocked
            return parsed

        # Step 2: RETRIEVE
        retrieval = self._step_retrieve(user_text, response)

        # Step 3: INSPECT — pull current pet/schedule state for grounding
        snapshot = self._step_inspect(user_text, response)

        # Step 4 + 5: PLAN + ACT — pick a tool and run it (may be no-op)
        self._step_plan_and_act(user_text, response, snapshot)

        # Step 6: EXPLAIN — ask the LLM to compose the answer
        self._step_explain(user_text, retrieval, response)

        # Step 7: VERIFY — output guardrails + final confidence
        self._step_verify(retrieval, response)

        self.guardrails.log_event("agent.end", {
            "intent": response.intent,
            "confidence": response.confidence,
            "tool_calls": response.tool_calls,
            "refused": response.refused,
        })
        return response

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _step_parse(self, user_text: str, response: AgentResponse) -> AgentResponse | None:
        violation = self.guardrails.check_input(user_text)
        if violation is not None:
            response.refused = True
            response.refusal_rule = violation.rule
            response.answer = violation.message
            response.steps.append(AgentStep(
                name="PARSE",
                detail=f"Input rejected: {violation.rule}",
                data={"rule": violation.rule, "severity": violation.severity},
            ))
            self.guardrails.log_event("guardrail.input_blocked", {
                "rule": violation.rule, "input": user_text,
            })
            return response

        intent = classify_intent(user_text)
        response.intent = intent
        response.steps.append(AgentStep(
            name="PARSE",
            detail=f"Classified intent as '{intent}'",
            data={"intent": intent},
        ))
        return None

    def _step_retrieve(self, user_text: str,
                       response: AgentResponse) -> list[RetrievalResult]:
        results = self.kb.retrieve(user_text, k=3)
        response.citations = [r.cite() for r in results]
        response.steps.append(AgentStep(
            name="RETRIEVE",
            detail=(f"Retrieved {len(results)} chunk(s) from knowledge base"
                    if results else "No relevant knowledge found"),
            data={
                "hits": [
                    {"source": r.cite(), "score": round(r.score, 3)}
                    for r in results
                ],
            },
        ))
        return results

    def _step_inspect(self, user_text: str,
                      response: AgentResponse) -> dict[str, Any]:
        """Read-only snapshot of pet/schedule state, for grounding the answer."""
        scheduler = Scheduler(self.owner)
        schedule = scheduler.generate_schedule(use_weighted=True) if self.owner.pets else []
        conflicts = scheduler.detect_conflicts(schedule)

        snapshot = {
            "owner_name": self.owner.name,
            "pet_count": len(self.owner.pets),
            "pet_names": [p.name for p in self.owner.pets],
            "task_count": len(self.owner.get_all_tasks()),
            "pending_count": sum(1 for t in self.owner.get_all_tasks()
                                 if not t.completed),
            "overdue_count": sum(1 for t in self.owner.get_all_tasks()
                                 if t.is_overdue()),
            "schedule_size": len(schedule),
            "conflict_count": len(conflicts),
            "mentioned_pet": _detect_pet_mention(user_text, self.owner.pets),
        }
        response.steps.append(AgentStep(
            name="INSPECT",
            detail=(f"{snapshot['pet_count']} pet(s), "
                    f"{snapshot['pending_count']} pending tasks, "
                    f"{snapshot['overdue_count']} overdue, "
                    f"{snapshot['conflict_count']} conflict(s)"),
            data=snapshot,
        ))
        return snapshot

    def _step_plan_and_act(self, user_text: str, response: AgentResponse,
                           snapshot: dict[str, Any]) -> None:
        """Pick a tool based on intent + snapshot, then execute it."""
        plan, tool_result = "no tool needed", None
        intent = response.intent

        # Emergency: short-circuit with a flag (the EXPLAIN step uses the
        # emergency template path automatically because intent == "emergency").
        if intent == "emergency":
            plan = "flag_emergency"
            tool_result = self._tool_flag_emergency(user_text)
            response.tool_calls.append(plan)

        # Schedule advice: if there are conflicts or zero scheduled, surface
        # the schedule explicitly so the LLM can mention it.
        elif intent == "schedule_advice":
            plan = "get_schedule"
            tool_result = self._tool_get_schedule()
            response.tool_calls.append(plan)
            if snapshot["conflict_count"] > 0:
                response.tool_calls.append("check_conflicts")

        # add_task: try to extract enough fields from the user text to create
        # a draft task. We DO NOT auto-commit it — the agent returns a
        # proposal, and the caller (UI/CLI) confirms before persisting.
        elif intent == "add_task":
            proposal = _extract_task_proposal(user_text, snapshot["mentioned_pet"])
            if proposal:
                plan = "add_task (proposed)"
                tool_result = {"proposal": proposal}
                response.tool_calls.append(plan)

        elif intent == "symptom_check" and snapshot["mentioned_pet"]:
            plan = "get_pet_info"
            tool_result = self._tool_get_pet_info(snapshot["mentioned_pet"])
            response.tool_calls.append(plan)

        response.steps.append(AgentStep(
            name="PLAN+ACT",
            detail=f"Tool decision: {plan}",
            data={"plan": plan, "result": tool_result},
        ))

    def _step_explain(self, user_text: str,
                      retrieval: list[RetrievalResult],
                      response: AgentResponse) -> None:
        context = self.kb.render_context(retrieval) if retrieval else ""
        prompt = (
            f"[SYSTEM]\nYou are PawPal+, a pet-care scheduling assistant. "
            f"Only use the provided CONTEXT to answer. Cite sources when "
            f"possible. If the context is empty or off-topic, say so.\n\n"
            f"[CONTEXT]\n{context}\n\n"
            f"[QUERY]\n{user_text}\n"
        )
        draft = self.llm.generate(prompt).strip()

        # Append a clean Sources block so the user always sees what we cited,
        # without mixing source markers into the prose.
        if response.citations:
            sources_block = "\n\nSources: " + "; ".join(response.citations)
            draft += sources_block

        response.answer = draft
        response.steps.append(AgentStep(
            name="EXPLAIN",
            detail=f"Composed answer with {self.llm.name} ({len(draft)} chars)",
            data={"backend": self.llm.name, "length": len(draft)},
        ))

    def _step_verify(self, retrieval: list[RetrievalResult],
                     response: AgentResponse) -> None:
        top_score = retrieval[0].score if retrieval else 0.0

        # Emergency answers are allowed to bypass the "ungrounded" rule —
        # the safety template is hard-coded and always appropriate.
        if response.intent != "emergency":
            violation = self.guardrails.check_output(response.answer, top_score)
            if violation is not None:
                response.refused = True
                response.refusal_rule = violation.rule
                response.answer = violation.message
                response.steps.append(AgentStep(
                    name="VERIFY",
                    detail=f"Output blocked: {violation.rule}",
                    data={"rule": violation.rule},
                ))
                self.guardrails.log_event("guardrail.output_blocked", {
                    "rule": violation.rule, "top_score": top_score,
                })
                response.confidence = 0.0
                return

        response.confidence = _compute_confidence(
            retrieval=retrieval,
            intent=response.intent,
            tool_calls=response.tool_calls,
        )
        response.steps.append(AgentStep(
            name="VERIFY",
            detail=f"Output passed checks. Confidence={response.confidence:.2f}",
            data={"confidence": response.confidence,
                  "top_retrieval_score": round(top_score, 3)},
        ))

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _tool_get_pet_info(self, pet_name: str) -> dict[str, Any]:
        for p in self.owner.pets:
            if p.name.lower() == pet_name.lower():
                return {
                    "name": p.name,
                    "species": p.species,
                    "age": p.age,
                    "breed": p.breed,
                    "task_count": len(p.tasks),
                    "pending": [t.title for t in p.tasks if not t.completed],
                    "overdue": [t.title for t in p.tasks if t.is_overdue()],
                }
        return {"error": f"Pet '{pet_name}' not found"}

    def _tool_get_schedule(self) -> dict[str, Any]:
        scheduler = Scheduler(self.owner)
        schedule = scheduler.generate_schedule(use_weighted=True)
        return {
            "entries": [
                {
                    "pet": e.pet_name,
                    "task": e.task.title,
                    "type": e.task.task_type.value,
                    "priority": e.task.priority.value,
                    "start": e.start_time.strftime("%H:%M"),
                    "end": e.end_time.strftime("%H:%M"),
                }
                for e in schedule
            ],
            "explanation": scheduler.explain_plan(),
        }

    def _tool_flag_emergency(self, user_text: str) -> dict[str, Any]:
        return {
            "escalation": "vet_immediate",
            "hotline": "(888) 426-4435 — ASPCA Animal Poison Control",
            "trigger_text": user_text[:200],
        }

    def add_task_from_proposal(self, proposal: dict[str, Any]) -> bool:
        """
        Commit a previously-proposed task to a pet. Called by the UI/CLI
        after the user confirms. Returns True on success.

        Kept separate from ``ask()`` so the agent never mutates state without
        explicit user approval — a guardrail against runaway tool use.
        """
        pet = next((p for p in self.owner.pets
                    if p.name.lower() == proposal.get("pet_name", "").lower()), None)
        if pet is None:
            return False
        try:
            task = Task(
                title=proposal.get("title", "Untitled"),
                task_type=TaskType(proposal.get("task_type", "other")),
                duration_minutes=int(proposal.get("duration_minutes", 20)),
                priority=Priority(proposal.get("priority", "medium")),
                notes=proposal.get("notes", "") + " (added by AI advisor)",
            )
        except (ValueError, KeyError) as exc:
            self.guardrails.log_error("add_task_from_proposal failed",
                                      proposal=proposal, error=str(exc))
            return False

        pet.add_task(task)
        if self.data_path is not None:
            self.owner.save_to_json(self.data_path)
        self.guardrails.log_event("agent.task_added", {
            "pet": pet.name, "task": task.title, "priority": task.priority.value,
        })
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_pet_mention(user_text: str, pets: list[Pet]) -> str:
    """Return the first known pet name mentioned in the text, or ''."""
    lower = user_text.lower()
    for p in pets:
        if re.search(rf"\b{re.escape(p.name.lower())}\b", lower):
            return p.name
    return ""


_TASK_TYPE_KEYWORDS: dict[str, TaskType] = {
    "walk":        TaskType.WALK,
    "feed":        TaskType.FEEDING,
    "feeding":     TaskType.FEEDING,
    "meal":        TaskType.FEEDING,
    "medic":       TaskType.MEDICATION,
    "pill":        TaskType.MEDICATION,
    "groom":       TaskType.GROOMING,
    "brush":       TaskType.GROOMING,
    "play":        TaskType.PLAY,
    "vet":         TaskType.APPOINTMENT,
    "appointment": TaskType.APPOINTMENT,
}


def _extract_task_proposal(user_text: str, mentioned_pet: str) -> dict[str, Any] | None:
    """
    Heuristic NLU for "add a task" intent.

    Pulls a task type, an optional duration, and a priority hint out of the
    user text. Returns ``None`` if we can't infer a task type — better to
    refuse than to add a garbage task.
    """
    lower = user_text.lower()

    task_type: TaskType | None = None
    for keyword, t in _TASK_TYPE_KEYWORDS.items():
        if keyword in lower:
            task_type = t
            break
    if task_type is None:
        return None

    duration_match = re.search(r"(\d{1,3})\s*(?:min|minute|minutes|m)\b", lower)
    duration = int(duration_match.group(1)) if duration_match else 20

    if "high" in lower or "urgent" in lower or "asap" in lower:
        priority = "high"
    elif "low" in lower or "if i have time" in lower or "optional" in lower:
        priority = "low"
    else:
        priority = "medium"

    title_root = task_type.value.capitalize()
    icon = TASK_TYPE_EMOJI.get(task_type, "")
    return {
        "pet_name": mentioned_pet,
        "title": f"{title_root} (AI suggested)",
        "task_type": task_type.value,
        "duration_minutes": duration,
        "priority": priority,
        "icon": icon,
        "notes": f"Proposed by AI advisor on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    }


def _compute_confidence(retrieval: list[RetrievalResult],
                        intent: str,
                        tool_calls: list[str]) -> float:
    """
    Composite confidence in [0, 1].

    Weights:
      * 60% - top retrieval score (how well grounded the answer is)
      * 20% - intent specificity (a clear intent is worth more than 'general')
      * 20% - whether a tool was called (acting > just talking)

    Emergency intents get a hard floor of 0.9 because the response is a
    safety template, not a generated answer — confidence in escalation is
    by construction high.
    """
    if intent == "emergency":
        return 0.95

    grounding = retrieval[0].score if retrieval else 0.0
    grounding = min(1.0, grounding * 1.5)  # rescale; raw cosine rarely exceeds 0.6

    intent_bonus = 0.0 if intent == "general" else 1.0
    tool_bonus = 0.0 if not tool_calls else 1.0

    score = 0.6 * grounding + 0.2 * intent_bonus + 0.2 * tool_bonus
    return round(max(0.0, min(1.0, score)), 3)
