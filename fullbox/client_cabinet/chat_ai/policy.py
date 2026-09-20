"""Политика действий ИИ: что можно предлагать / нельзя делать автоматически."""

from __future__ import annotations

from dataclasses import dataclass

from ..models import ChatAISettings, ChatThread


# Темы, где автоответ запрещён навсегда (даже после этапа 3).
AUTO_REPLY_BLOCKED_CATEGORIES = frozenset(
    {
        "претензия",
        "тарифы",
        "биллинг",
        "расхождения",
        "брак",
        "другое",  # ambiguous — no auto
    }
)

# Ключевые слова → грубая классификация (stub, без LLM).
CATEGORY_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("претензия", ("претенз", "компенсац", "недовол", "ужас", "возврат денег")),
    ("биллинг", ("счёт", "счет", "акт", "оплат", "задолжен", "упд")),
    ("тарифы", ("тариф", "цен", "стоимост", "скидк")),
    ("остатки", ("остат", "наличи", "сколько на склад")),
    ("отгрузка", ("отгруз", "shipping", "отправ")),
    ("приёмка", ("приёмк", "приемк", "поставк")),
    ("обработка", ("обработ", "фасов")),
    ("маркировка", ("маркир", "честн", "киз", "чз")),
    ("логистика", ("логист", "рейс", "доставк")),
    ("маркетплейсы", ("ozon", "wildberries", "wb ", "маркетплейс")),
    ("документы", ("документ", "договор", "реквизит")),
    ("расхождения", ("расхожд", "недостач", "излиш", "брак")),
]


@dataclass
class AIActionPolicy:
    """Проверки до вызова модели и перед автоответом."""

    role: str = ""
    agency_id: int | None = None
    thread: ChatThread | None = None

    def settings(self) -> ChatAISettings:
        return ChatAISettings.load()

    def copilot_allowed(self) -> tuple[bool, str]:
        cfg = self.settings()
        if not cfg.copilot_enabled:
            return False, "ИИ-помощник выключен администратором"
        if self.role in {
            "storekeeper",
            "picker",
            "processing_worker",
            "processing_head",
            "reachtruck_driver",
            "super_car",
        }:
            # Этап 1: только менеджерский copilot
            return False, "На первом этапе ИИ доступен менеджерам, не складу"
        if not self.thread or not self.agency_id:
            return False, "Нет контекста чата/клиента"
        if int(self.thread.agency_id) != int(self.agency_id):
            return False, "Нарушение изоляции клиента"
        return True, ""

    def auto_reply_allowed(self, *, category: str, confidence: str) -> bool:
        cfg = self.settings()
        if not cfg.auto_reply_enabled:
            return False
        if confidence != "high":
            return False
        if (category or "").strip().lower() in AUTO_REPLY_BLOCKED_CATEGORIES:
            return False
        return False  # этап 1: всегда выключено явно

    @staticmethod
    def classify_stub(text: str) -> str:
        low = (text or "").lower()
        for category, hints in CATEGORY_HINTS:
            if any(h in low for h in hints):
                return category
        return "другое"

    @staticmethod
    def forbidden_actions() -> list[str]:
        return [
            "change_stock",
            "create_movement",
            "change_order_status",
            "create_invoice",
            "change_tariff",
            "close_order",
        ]
