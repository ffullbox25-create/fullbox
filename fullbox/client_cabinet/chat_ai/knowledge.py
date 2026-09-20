"""Поиск по базе знаний (stub). Позже — RAG / pgvector."""

from __future__ import annotations

from .types import AISource

# Утверждённые безопасные подсказки для черновика (не автоответ клиенту).
STUB_KB: list[dict[str, str]] = [
    {
        "category": "остатки",
        "title": "Остатки в ЛК",
        "text": "Актуальные остатки смотрите в разделе «Остатки» личного кабинета. Там показано доступное к отгрузке количество.",
    },
    {
        "category": "документы",
        "title": "Документы и акты",
        "text": "Счета и акты доступны в разделе «Финансы / Биллинг» личного кабинета. Если документа нет — уточните у менеджера номер заявки.",
    },
    {
        "category": "приёмка",
        "title": "Создание заявки на приёмку",
        "text": "Создать заявку на приёмку можно в личном кабинете в разделе заявок. Укажите дату, состав и документы поставки.",
    },
    {
        "category": "отгрузка",
        "title": "Заявка на отгрузку",
        "text": "Заявку на отгрузку создайте в личном кабинете. Для маркетплейсов заполните склад назначения и состав коробов по инструкции.",
    },
]


def search_knowledge_stub(*, category: str = "", query: str = "", limit: int = 3) -> list[AISource]:
    cat = (category or "").strip().lower()
    q = (query or "").strip().lower()
    hits: list[AISource] = []
    for row in STUB_KB:
        if cat and row["category"] != cat and cat != "другое":
            continue
        if q and q not in row["text"].lower() and q not in row["title"].lower():
            if cat != row["category"]:
                continue
        hits.append(
            AISource(
                title=row["title"],
                kind="knowledge",
                ref=f"stub:{row['category']}",
                updated_at="",
            )
        )
        if len(hits) >= limit:
            break
    if not hits and cat and cat != "другое":
        for row in STUB_KB:
            if row["category"] == cat:
                hits.append(
                    AISource(title=row["title"], kind="knowledge", ref=f"stub:{row['category']}")
                )
                break
    return hits


def kb_answer_for(category: str) -> str:
    for row in STUB_KB:
        if row["category"] == category:
            return row["text"]
    return ""
