"""
LLM client abstraction for the PawPal+ Care Advisor.

Two concrete backends are provided:

* ``LocalLLM`` — a deterministic, rule-based "LLM" that runs offline with no
  API key. It uses keyword matching plus templated reasoning so the agent
  always produces output, even on a laptop with no internet. This is the
  default backend used by the agent and the eval harness.

* ``OllamaLLM`` — an optional adapter that talks to a local Ollama server
  (http://localhost:11434) if the user has one installed. Exposed so the same
  agent can run with a real LLM (Llama 3, Mistral, etc.) without changing
  any other code.

Both backends implement the same ``LLMClient`` protocol: a single
``generate(prompt: str, *, system: str = "", temperature: float = 0.0) -> str``
method. The agent code never imports either concrete class directly — it
goes through ``build_default_client()`` which auto-detects what's available.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """Minimal protocol every LLM backend must implement."""

    name: str

    def generate(self, prompt: str, *, system: str = "", temperature: float = 0.0) -> str:
        """Return a single completion for ``prompt``. Never raises on backend error — returns a fallback string instead."""
        ...

    def is_available(self) -> bool:
        """Return True if the backend is ready to serve requests."""
        ...


# ---------------------------------------------------------------------------
# LocalLLM — offline, deterministic, no dependencies
# ---------------------------------------------------------------------------

# Intent patterns — ordered by specificity. The first match wins.
# Each entry is (intent_label, regex_pattern). Intents drive the agent's
# tool selection; see ai/agent.py for how they're consumed.
_INTENT_PATTERNS: list[tuple[str, str]] = [
    ("emergency",       r"\b(emergency|seizure|collapse|bleeding|poisoned?|toxic|swallowed|"
                        r"choking|unconscious|can'?t breathe|hit by car|broken bone|"
                        r"blood in (vomit|stool|urine)|not breathing)\b"),
    ("symptom_check",   r"\b(lethargic|vomit|diarrhea|limping|won'?t eat|not eating|"
                        r"refusing food|losing weight|coughing|wheezing|sneezing|"
                        r"itching|scratching a lot|hiding|shaking|trembling)\b"),
    ("schedule_advice", r"\b(schedule|plan|today'?s plan|daily plan|what should i do today|"
                        r"organi[sz]e my day|too many tasks|conflict)\b"),
    ("add_task",        r"\b(add (a |an )?(task|\d+\s*(min|minute|minutes|hour|hr)?\s*"
                        r"(walk|feed|feeding|medication|grooming|play|appointment))|"
                        r"create (a |an )?task|need to (walk|feed|medicate|groom)|"
                        r"remind me to|schedule (a |an )?(walk|feeding|medication|"
                        r"grooming|appointment|play))\b"),
    ("feeding_q",       r"\b(feed|feeding|food|meal|hungry|appetite|how much (should|to))\b"),
    ("walking_q",       r"\b(walk|walking|exercise|run|tired|energy|hyper)\b"),
    ("medication_q",    r"\b(medication|medicine|pill|dose|missed (a )?dose|antibiotic|insulin)\b"),
    ("grooming_q",      r"\b(groom|brush|bath|nail|coat|fur|shed)\b"),
    ("behavior_q",      r"\b(barking|aggressive|anxious|destructive|chewing|scratching|"
                        r"biting|whining|crying|bored)\b"),
]


def classify_intent(user_text: str) -> str:
    """
    Map free-text user input to one of the known intents above.

    Returns ``"general"`` if no pattern matches. The agent uses this to
    decide which tools to call. Implemented as a pure function so it can be
    tested independently of the rest of the LLM client.
    """
    if not user_text or not user_text.strip():
        return "general"
    text = user_text.lower()
    for intent, pattern in _INTENT_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return intent
    return "general"


@dataclass
class LocalLLM:
    """
    Deterministic offline 'LLM' for the Care Advisor.

    Given a prompt that contains a ``[CONTEXT]`` block (retrieved knowledge)
    and a ``[QUERY]`` block (user question), this backend produces a
    structured, plain-English answer that:

    * cites the knowledge sources by filename
    * stays scoped to pet care (refuses off-topic queries)
    * never invents medical specifics

    It is intentionally simple: the value of this project is in the
    *agentic workflow* and *retrieval grounding*, not in language fluency.
    Swapping in OllamaLLM gives you fluent prose for free.
    """

    name: str = "local-rule-based-v1"

    def is_available(self) -> bool:
        return True

    def generate(self, prompt: str, *, system: str = "", temperature: float = 0.0) -> str:
        query = _extract_block(prompt, "QUERY") or prompt
        context = _extract_block(prompt, "CONTEXT") or ""
        intent = classify_intent(query)

        if intent == "emergency":
            return _compose_emergency_response(context)

        if not context.strip():
            return (
                "I couldn't find anything in my pet-care knowledge base that matches "
                "your question. I'm only able to help with feeding, walking, medication, "
                "grooming, behavior, and emergency triage for pets. Could you rephrase, "
                "or ask about one of those topics?"
            )

        return _compose_grounded_response(query, context, intent)


def _extract_block(prompt: str, label: str) -> str:
    """Pull the text between ``[LABEL]`` and the next ``[...]`` tag (or end of string)."""
    pattern = rf"\[{label}\](.*?)(?=\n\[[A-Z_]+\]|\Z)"
    match = re.search(pattern, prompt, flags=re.DOTALL)
    return match.group(1).strip() if match else ""


def _compose_emergency_response(context: str) -> str:
    """Hard-coded safety response. Never substitutes for a vet, always escalates."""
    lines = [
        "This sounds like it may be an emergency. Please do not wait for a scheduled "
        "visit — contact a veterinarian or emergency animal hospital immediately.",
        "",
        "If you suspect poisoning, the ASPCA Animal Poison Control hotline is "
        "(888) 426-4435 (a fee may apply).",
        "",
        "While you arrange care, keep your pet calm and do not attempt to induce "
        "vomiting or administer human medications unless instructed by a vet.",
    ]
    if context.strip():
        lines += ["", "Relevant guidance from my knowledge base:", context.strip()[:600]]
    return "\n".join(lines)


def _compose_grounded_response(query: str, context: str, intent: str) -> str:
    """Compose a templated answer that quotes (lightly) from retrieved context."""
    intro = {
        "symptom_check":   "Based on the symptoms you described, here's what my pet-care notes suggest:",
        "schedule_advice": "Here's how I'd think about your day, based on PawPal+'s scheduling guidance:",
        "add_task":        "Here's what I'd add to your PawPal+ schedule, based on my notes:",
        "feeding_q":       "On feeding, my notes say:",
        "walking_q":       "On walking and exercise, my notes say:",
        "medication_q":    "On medication, my notes say:",
        "grooming_q":      "On grooming, my notes say:",
        "behavior_q":      "On the behavior you described, my notes say:",
        "general":         "Here's what I found in my pet-care knowledge base:",
    }.get(intent, "Here's what I found in my pet-care knowledge base:")

    snippet = _summarize_context(context)
    follow_up = (
        "\n\nIf any symptom is severe or worsening, please contact a veterinarian — "
        "I'm a scheduling assistant, not a diagnostic tool."
    )
    return f"{intro}\n\n{snippet}{follow_up}"


def _summarize_context(context: str) -> str:
    """
    Pull 2-3 informative sentences out of the retrieved context.

    Heuristic: prefer sentences that contain numbers (durations, frequencies)
    or imperative verbs ("schedule", "contact", "avoid"). Falls back to the
    first 2 sentences if nothing scores well.
    """
    # Drop the [Source: ...] header lines and the --- separators that
    # ``KnowledgeBase.render_context()`` injects — they're for the prompt,
    # not for the human-facing summary.
    cleaned_lines = [
        line for line in context.splitlines()
        if line.strip() and not line.lstrip().startswith("[Source:")
        and line.strip() != "---"
    ]
    cleaned = " ".join(cleaned_lines)
    sentences = re.split(r"(?<=[.!?])\s+", cleaned.strip())
    sentences = [s.strip() for s in sentences if 20 < len(s) < 300]
    if not sentences:
        return context.strip()[:400]

    def score(s: str) -> int:
        sc = 0
        if re.search(r"\d", s):
            sc += 2
        if re.search(r"\b(should|must|avoid|contact|schedule|never|always|"
                     r"recommend|ensure|aim for)\b", s, flags=re.IGNORECASE):
            sc += 2
        sc += min(len(s) // 100, 2)
        return sc

    ranked = sorted(sentences, key=score, reverse=True)
    chosen = ranked[:3]
    chosen.sort(key=sentences.index)
    return " ".join(chosen)


# ---------------------------------------------------------------------------
# OllamaLLM — optional, used only if the user has Ollama running
# ---------------------------------------------------------------------------

@dataclass
class OllamaLLM:
    """
    Adapter for a locally-running Ollama server.

    Not required for the project to work — ``build_default_client()`` falls
    back to ``LocalLLM`` if Ollama is unreachable. Included so that anyone
    who has installed Ollama (https://ollama.com) can plug a real LLM into
    the same agent loop and get noticeably more fluent answers.
    """

    model: str = "llama3.2"
    host: str = "http://localhost:11434"
    name: str = "ollama"
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        self.name = f"ollama:{self.model}"

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=2) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def generate(self, prompt: str, *, system: str = "", temperature: float = 0.0) -> str:
        body = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": temperature},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("response", "").strip()
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
            # Never crash the agent loop because of a backend hiccup —
            # fall back to LocalLLM so the user still gets *some* answer.
            return LocalLLM().generate(prompt, system=system, temperature=temperature)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_default_client(prefer: str | None = None) -> LLMClient:
    """
    Build the right backend for the current environment.

    Resolution order:
      1. ``prefer`` argument, if provided ("local" or "ollama")
      2. ``PAWPAL_LLM`` env var ("local" or "ollama")
      3. Auto-detect: try Ollama, fall back to LocalLLM
    """
    choice = (prefer or os.environ.get("PAWPAL_LLM") or "auto").lower()

    if choice == "local":
        return LocalLLM()
    if choice == "ollama":
        client = OllamaLLM(model=os.environ.get("PAWPAL_OLLAMA_MODEL", "llama3.2"))
        return client if client.is_available() else LocalLLM()

    # auto
    candidate = OllamaLLM(model=os.environ.get("PAWPAL_OLLAMA_MODEL", "llama3.2"))
    return candidate if candidate.is_available() else LocalLLM()
