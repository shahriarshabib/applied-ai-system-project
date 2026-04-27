"""
Input/output guardrails and structured logging for the Care Advisor agent.

The agent calls ``Guardrails.check_input()`` before any retrieval or tool
use, and ``Guardrails.check_output()`` before returning the final answer.
Violations short-circuit the agent and return a safe canned message so we
never serve a half-grounded or unsafe response.

Logging is JSON-lines to ``logs/agent.jsonl`` so the eval harness and any
future dashboard can replay every interaction without parsing free text.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class GuardrailViolation:
    """Returned when input or output fails a guardrail check."""
    rule: str
    message: str
    severity: str  # "info" | "warn" | "block"


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

# Patterns that indicate the user is asking PawPal+ to do something far
# outside its scope (write code, do math homework, talk politics, etc.).
# These are blocked at the input stage so we never even retrieve for them.
_OFF_TOPIC = re.compile(
    r"\b(write\s+(me\s+)?(a\s+)?(poem|essay|code|python|javascript|sql)|"
    r"hack|exploit|bypass|jailbreak|"
    r"presidential|election|stock|crypto|bitcoin|"
    r"recipe for (humans?|me))\b",
    re.IGNORECASE,
)

# Prompt-injection phrases that try to override the system instructions.
_INJECTION = re.compile(
    r"(ignore (all|previous|the above) instructions|"
    r"you are now|act as if|disregard your|"
    r"system prompt|reveal your prompt|print your (system )?prompt)",
    re.IGNORECASE,
)

# Output content that we never want to ship: specific drug doses, claims of
# diagnosis, or any instruction to skip a vet for a serious symptom.
_UNSAFE_OUTPUT = re.compile(
    r"\b(\d+\s*(mg|ml|cc)\s*(per|/)\s*(kg|lb|pound))\b|"
    r"\b(diagnose[ds]? with|you have|your pet has)\s+\w+ (disease|cancer|infection)\b|"
    r"\b(don'?t need (to|a) (see|call|visit)\s+(a\s+)?vet)\b",
    re.IGNORECASE,
)


class Guardrails:
    """Stateless guardrail checks plus JSONL logging."""

    MAX_INPUT_CHARS = 1500
    MIN_INPUT_CHARS = 3
    MIN_RETRIEVAL_SCORE = 0.05  # below this we refuse to answer

    def __init__(self, log_path: str | Path = "logs/agent.jsonl") -> None:
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Standard logger for human-readable lines (errors, traces).
        self._logger = logging.getLogger("pawpal.ai")
        if not self._logger.handlers:
            self._logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            self._logger.addHandler(handler)

    # ------------------------------------------------------------------
    # Input guardrails — run BEFORE retrieval and tool use
    # ------------------------------------------------------------------

    def check_input(self, user_text: str) -> GuardrailViolation | None:
        """
        Validate the raw user input. Returns ``None`` if safe to proceed,
        otherwise returns a ``GuardrailViolation`` whose ``message`` is
        suitable for display to the user.
        """
        if user_text is None or not user_text.strip():
            return GuardrailViolation(
                rule="empty_input",
                message="I didn't receive a question. Please describe what you need help with.",
                severity="block",
            )
        if len(user_text) < self.MIN_INPUT_CHARS:
            return GuardrailViolation(
                rule="too_short",
                message="Your question is too short for me to understand. "
                        "Could you give me a bit more detail?",
                severity="block",
            )
        if len(user_text) > self.MAX_INPUT_CHARS:
            return GuardrailViolation(
                rule="too_long",
                message=f"Please keep questions under {self.MAX_INPUT_CHARS} characters. "
                        "Long inputs are harder to ground in my knowledge base.",
                severity="block",
            )
        if _INJECTION.search(user_text):
            return GuardrailViolation(
                rule="prompt_injection",
                message="I can't follow instructions that try to override my role. "
                        "I'm a pet-care scheduling assistant — what can I help you with?",
                severity="block",
            )
        if _OFF_TOPIC.search(user_text):
            return GuardrailViolation(
                rule="off_topic",
                message="I'm scoped to pet care: feeding, walking, medication, grooming, "
                        "behavior, and emergency triage. Anything else is outside what I "
                        "can responsibly answer.",
                severity="block",
            )
        return None

    # ------------------------------------------------------------------
    # Output guardrails — run BEFORE returning the final response
    # ------------------------------------------------------------------

    def check_output(self, response_text: str,
                     retrieval_score: float) -> GuardrailViolation | None:
        """
        Validate the model's draft answer against safety rules.

        ``retrieval_score`` is the highest score from the RAG retriever.
        If it's below ``MIN_RETRIEVAL_SCORE`` we treat the answer as
        ungrounded and refuse — this is the single most important
        hallucination guardrail.
        """
        if retrieval_score < self.MIN_RETRIEVAL_SCORE:
            return GuardrailViolation(
                rule="ungrounded",
                message="I couldn't find anything relevant in my knowledge base, so I'd "
                        "rather not guess. Try rephrasing, or ask about feeding, walking, "
                        "medication, grooming, behavior, or emergencies.",
                severity="block",
            )
        if not response_text or not response_text.strip():
            return GuardrailViolation(
                rule="empty_output",
                message="I wasn't able to generate a response. Please try again.",
                severity="block",
            )
        if _UNSAFE_OUTPUT.search(response_text):
            return GuardrailViolation(
                rule="unsafe_content",
                message="I started to give specific medical guidance, but that's outside "
                        "what I should do. Please consult your veterinarian for dosing or "
                        "diagnostic questions.",
                severity="block",
            )
        return None

    # ------------------------------------------------------------------
    # Structured logging
    # ------------------------------------------------------------------

    def log_event(self, event: str, payload: dict[str, Any]) -> None:
        """Append one JSON line describing an agent event."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError as exc:
            # Logging must never crash the agent.
            self._logger.warning("Failed to write agent log: %s", exc)

    def log_error(self, message: str, **context: Any) -> None:
        """Log an unexpected exception or recoverable error."""
        self._logger.error(message + " | %s", context)
        self.log_event("error", {"message": message, **context})
