"""ИИ-агент чатов FullBox: сервисный слой (этап 1 — stub copilot)."""

from .orchestrator import apply_suggestion_feedback, suggest_reply_for_thread
from .policy import AIActionPolicy

__all__ = [
    "AIActionPolicy",
    "apply_suggestion_feedback",
    "suggest_reply_for_thread",
]
