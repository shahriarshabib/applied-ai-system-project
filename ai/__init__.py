"""PawPal+ Applied AI System — agentic + RAG layer over the PawPal+ scheduler."""

from ai.llm_client import LLMClient, LocalLLM, OllamaLLM, build_default_client
from ai.knowledge import KnowledgeBase, RetrievalResult
from ai.agent import CareAdvisorAgent, AgentResponse, AgentStep
from ai.guardrails import Guardrails, GuardrailViolation

__all__ = [
    "LLMClient",
    "LocalLLM",
    "OllamaLLM",
    "build_default_client",
    "KnowledgeBase",
    "RetrievalResult",
    "CareAdvisorAgent",
    "AgentResponse",
    "AgentStep",
    "Guardrails",
    "GuardrailViolation",
]
