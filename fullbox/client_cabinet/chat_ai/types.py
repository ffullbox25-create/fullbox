"""Структурированный выход ИИ (не свободный текст для критических решений)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class AISource:
    title: str
    kind: str = "note"  # knowledge | order | faq | note
    ref: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AISuggestResult:
    text: str
    category: str = "другое"
    confidence: str = "low"  # high | medium | low
    sources: list[AISource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    needs_escalation: bool = False
    auto_reply_allowed: bool = False
    model_name: str = "stub"

    def to_payload(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "category": self.category,
            "confidence": self.confidence,
            "sources": [s.to_dict() for s in self.sources],
            "warnings": list(self.warnings),
            "suggested_actions": list(self.suggested_actions),
            "needs_escalation": bool(self.needs_escalation),
            "auto_reply_allowed": bool(self.auto_reply_allowed),
            "model_name": self.model_name,
        }
