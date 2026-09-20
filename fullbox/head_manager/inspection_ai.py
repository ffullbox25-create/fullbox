"""Read-only incident aggregation for the head-manager inspection screen.

This module deliberately contains no write-path calls.  It reads existing
diagnostics and converts them into deterministic incident cards.  Any source
failure is isolated so the inspection screen cannot break the warehouse UI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from time import perf_counter
from typing import Callable, Iterable

from django.utils import timezone


SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
SEVERITY_ORDER = {
    SEVERITY_CRITICAL: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_INFO: 2,
}
EVIDENCE_LIMIT = 10


def _short(value, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _format_dt(value) -> str:
    if not value:
        return "—"
    try:
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return value.strftime("%d.%m.%Y %H:%M:%S")
    except (AttributeError, TypeError, ValueError):
        return _short(value, 64) or "—"


def _age_text(now: datetime, value) -> str:
    if not value:
        return "время не зафиксировано"
    try:
        seconds = max(int((now - value).total_seconds()), 0)
    except (TypeError, ValueError):
        return "возраст не определён"
    if seconds >= 86400:
        return f"{seconds // 86400} дн. {seconds % 86400 // 3600} ч."
    if seconds >= 3600:
        return f"{seconds // 3600} ч. {seconds % 3600 // 60} мин."
    return f"{max(seconds // 60, 1)} мин."


def _evidence(
    title: str,
    *,
    subtitle: str = "",
    facts: Iterable[str] = (),
    message: str = "",
) -> dict:
    return {
        "title": _short(title, 180) or "Объект без названия",
        "subtitle": _short(subtitle, 240),
        "facts": tuple(_short(item, 180) for item in facts if _short(item, 180)),
        "message": _short(message, 500),
    }


def _payload_message(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("error", "message", "detail", "reason", "status_message"):
        value = payload.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return _short(value, 500)
    return ""


@dataclass(frozen=True, slots=True)
class InspectionIncident:
    code: str
    severity: str
    contour: str
    title: str
    message: str
    recommendation: str
    source: str
    count: int = 0
    why_it_matters: str = ""
    check_rule: str = ""
    evidence: tuple[dict, ...] = ()
    evidence_total: int = 0
    requires_human: bool = True
    automatic_action: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _incident(
    *,
    code: str,
    severity: str,
    contour: str,
    title: str,
    message: str,
    recommendation: str,
    source: str,
    count: int = 0,
    why_it_matters: str = "",
    check_rule: str = "",
    evidence: Iterable[dict] = (),
    evidence_total: int | None = None,
) -> InspectionIncident:
    normalized_severity = (
        severity if severity in SEVERITY_ORDER else SEVERITY_WARNING
    )
    normalized_evidence = tuple(dict(item) for item in evidence if item)
    return InspectionIncident(
        code=str(code or "unknown"),
        severity=normalized_severity,
        contour=str(contour or "WMS"),
        title=str(title or "Требуется проверка"),
        message=str(message or "Источник сообщил о проблеме."),
        recommendation=str(
            recommendation
            or "Проверить вручную. Автоматические изменения запрещены."
        ),
        source=str(source or "unknown"),
        count=max(int(count or 0), 0),
        why_it_matters=_short(
            why_it_matters
            or "Сигнал может влиять на работу оператора или достоверность учёта.",
            600,
        ),
        check_rule=_short(
            check_rule
            or "Проверка выполнена по данным WMS на момент формирования отчёта.",
            600,
        ),
        evidence=normalized_evidence,
        evidence_total=max(
            int(
                len(normalized_evidence)
                if evidence_total is None
                else evidence_total
            ),
            len(normalized_evidence),
        ),
    )


FBS_RECOMMENDATIONS = {
    "module": "Проверить согласованное состояние флага FBS. Самостоятельно не включать.",
    "warehouse_writes": (
        "Проверить согласованное состояние складских операций FBS. "
        "Самостоятельно не включать."
    ),
    "profiles": "Проверить настройки профиля и последнюю успешную синхронизацию.",
    "workstations": "Проверить Desktop Agent, сканер и принтер на рабочих местах.",
    "totes": "Проверить свободную тару и привязки. Статусы автоматически не менять.",
    "waves": "Проверить ответственного и фактический этап зависших волн.",
    "outbox": "Разобрать ошибки команд WB/Ozon; повторять только идемпотентно и после подтверждения.",
    "labels": "Проверить агент печати, принтер и возраст заданий печати.",
    "scans": "Разложить ошибки по этапам и подсказать оператору правильный повторный скан.",
    "stock": "Передать программисту и начальнику склада. Остатки и резервы автоматически не исправлять.",
}

FBS_IMPACTS = {
    "module": "Операторы не смогут выполнять FBS-процесс через штатный модуль.",
    "warehouse_writes": "Складские действия FBS будут заблокированы защитным флагом.",
    "profiles": "Заказы или статусы клиента могут перестать синхронизироваться с маркетплейсом.",
    "workstations": "Рабочее место может не печатать и не принимать сканы.",
    "totes": "Новая волна может не получить свободную тару или использовать некорректную привязку.",
    "waves": "Отбор или контроль могут фактически остановиться, хотя волна остаётся активной.",
    "outbox": "Статус заказа в WMS и маркетплейсе может разойтись.",
    "labels": "Заказ нельзя безопасно передать дальше без корректной этикетки.",
    "scans": "Повторяющиеся ошибки сканирования замедляют оператора и повышают риск неверного товара.",
    "stock": "Доступное и зарезервированное количество не покрывается фактическим остатком.",
}

FBS_RULES = {
    "module": "Флаг модуля FBS должен быть включён для рабочего контура.",
    "warehouse_writes": "Защитный флаг складских операций FBS должен соответствовать согласованному режиму.",
    "profiles": "Активный профиль должен иметь реквизиты, склад маркетплейса и свежую успешную синхронизацию.",
    "workstations": "Должно существовать хотя бы одно активное и готовое рабочее место.",
    "totes": "Активная тара должна иметь допустимое состояние; для новой волны нужна свободная тара.",
    "waves": "Волна в работе или на проверке должна обновляться не реже одного раза в час.",
    "outbox": "Failed/conflict за 24 часа и pending/sent/retry старше часа считаются проблемой.",
    "labels": "Error за 24 часа и requested старше часа считаются проблемой.",
    "scans": "При пяти и более сканах доля ошибок за 24 часа не должна превышать 20%.",
    "stock": "Для каждой строки FBS: qty ≥ available_qty + reserved_qty.",
}


def _build_fbs_evidence(report: dict, now: datetime) -> dict[str, dict]:
    """Collect small, read-only samples for failed FBS checks."""

    from django.db.models import F, Q
    from fbs.models import (
        FbsMarketplaceCommand,
        FbsOrderLabel,
        FbsPickBatch,
        FbsPickScanEvent,
        FbsPickingCart,
        FbsStockBalance,
        FbsToteBinding,
    )

    day_ago = now - timedelta(hours=24)
    stale_work_at = now - timedelta(hours=1)
    result: dict[str, dict] = {}

    profile_rows = [
        row for row in (report.get("profiles") or []) if row.get("severity") != "ok"
    ]
    if profile_rows:
        result["profiles"] = {
            "total": len(profile_rows),
            "items": [
                _evidence(
                    f"Профиль #{row.get('id')} · {row.get('agency')}",
                    subtitle=(
                        f"{row.get('marketplace') or 'Маркетплейс не указан'} · "
                        f"склад {row.get('warehouse') or '—'}"
                    ),
                    facts=(
                        f"Последний успех: {_format_dt(row.get('last_success_at'))}",
                        f"Последний опрос: {_format_dt(row.get('last_polled_at'))}",
                    ),
                    message=row.get("message") or "Причина не указана",
                )
                for row in profile_rows[:EVIDENCE_LIMIT]
            ],
        }

    tote_items: list[dict] = []
    carts_without_binding = list(
        FbsPickingCart.objects.filter(is_active=True, binding__isnull=True)
        .order_by("id")[:EVIDENCE_LIMIT]
    )
    for cart in carts_without_binding:
        tote_items.append(
            _evidence(
                f"Тара #{cart.id} · {cart.name or 'Без названия'}",
                subtitle=cart.barcode,
                facts=("Привязка: отсутствует", f"Обновлено: {_format_dt(cart.updated_at)}"),
                message="Активная тара не имеет записи состояния/зоны.",
            )
        )
    remaining = EVIDENCE_LIMIT - len(tote_items)
    if remaining > 0:
        unbound_bindings = list(
            FbsToteBinding.objects.filter(
                tote__is_active=True,
                state=FbsToteBinding.STATE_UNBOUND,
            )
            .select_related("tote", "zone", "workstation")
            .order_by("id")[:remaining]
        )
        for binding in unbound_bindings:
            tote_items.append(
                _evidence(
                    f"Тара #{binding.tote_id} · {binding.tote.name or 'Без названия'}",
                    subtitle=binding.tote.barcode,
                    facts=(
                        f"Состояние: {binding.get_state_display()}",
                        f"Зона: {binding.zone or '—'}",
                        f"Рабочее место: {binding.workstation or '—'}",
                    ),
                    message="Привязка существует, но состояние отмечено как «не привязана».",
                )
            )
    if tote_items:
        result["totes"] = {
            "total": int(report.get("unbound_totes") or len(tote_items)),
            "items": tote_items,
        }

    stale_batches_qs = FbsPickBatch.objects.filter(
        status__in=(FbsPickBatch.STATUS_IN_PROGRESS, FbsPickBatch.STATUS_VERIFICATION),
        updated_at__lt=stale_work_at,
    ).select_related("agency", "assigned_to", "workstation", "cart")
    stale_batch_total = stale_batches_qs.count()
    if stale_batch_total:
        result["waves"] = {
            "total": stale_batch_total,
            "items": [
                _evidence(
                    f"Волна #{batch.id} · {batch.agency}",
                    subtitle=f"Статус: {batch.get_status_display()}",
                    facts=(
                        f"Ответственный: {batch.assigned_to or 'не назначен'}",
                        f"Рабочее место: {batch.workstation or '—'}",
                        f"Тара: {batch.cart or '—'}",
                        f"План/отобрано: {batch.planned_qty}/{batch.picked_qty}",
                        f"Последнее обновление: {_format_dt(batch.updated_at)} ({_age_text(now, batch.updated_at)} назад)",
                    ),
                )
                for batch in stale_batches_qs.order_by("updated_at", "id")[:EVIDENCE_LIMIT]
            ],
        }

    problem_commands_qs = FbsMarketplaceCommand.objects.filter(
        Q(
            status__in=(
                FbsMarketplaceCommand.STATUS_FAILED,
                FbsMarketplaceCommand.STATUS_CONFLICT,
            ),
            updated_at__gte=day_ago,
        )
        | Q(
            status__in=(
                FbsMarketplaceCommand.STATUS_PENDING,
                FbsMarketplaceCommand.STATUS_SENT,
                FbsMarketplaceCommand.STATUS_RETRY,
            ),
            updated_at__lt=stale_work_at,
        )
    ).select_related("profile__agency", "order")
    command_total = problem_commands_qs.count()
    if command_total:
        result["outbox"] = {
            "total": command_total,
            "items": [
                _evidence(
                    f"Команда #{command.id} · {command.command_type}",
                    subtitle=(
                        f"{command.profile.get_marketplace_display()} · "
                        f"{command.profile.agency} · заказ #{command.order_id or '—'}"
                    ),
                    facts=(
                        f"Статус: {command.get_status_display()}",
                        f"HTTP: {command.http_status or '—'}; попыток: {command.attempt_count}",
                        f"Метод/endpoint: {command.http_method} {_short(command.endpoint, 120)}",
                        f"Обновлено: {_format_dt(command.updated_at)} ({_age_text(now, command.updated_at)} назад)",
                    ),
                    message=command.error or "Текст ошибки отсутствует; проверьте статус и ответ маркетплейса.",
                )
                for command in problem_commands_qs.order_by("updated_at", "id")[:EVIDENCE_LIMIT]
            ],
        }

    problem_labels_qs = FbsOrderLabel.objects.filter(
        Q(status=FbsOrderLabel.STATUS_ERROR, updated_at__gte=day_ago)
        | Q(status=FbsOrderLabel.STATUS_REQUESTED, requested_at__lt=stale_work_at)
    ).select_related("order")
    label_total = problem_labels_qs.count()
    if label_total:
        result["labels"] = {
            "total": label_total,
            "items": [
                _evidence(
                    f"Этикетка #{label.id} · заказ #{label.order_id}",
                    subtitle=f"{label.get_marketplace_display()} · {label.get_status_display()}",
                    facts=(
                        f"Штрихкод: {_short(label.barcode, 100) or '—'}",
                        f"Запрошена: {_format_dt(label.requested_at)}",
                        f"Обновлена: {_format_dt(label.updated_at)}",
                    ),
                    message=label.error or "Этикетка ожидает ответ дольше установленного часа.",
                )
                for label in problem_labels_qs.order_by("updated_at", "id")[:EVIDENCE_LIMIT]
            ],
        }

    scan_errors_qs = FbsPickScanEvent.objects.filter(
        created_at__gte=day_ago,
        result=FbsPickScanEvent.RESULT_ERROR,
    ).select_related("batch", "created_by")
    scan_error_total = scan_errors_qs.count()
    if scan_error_total:
        result["scans"] = {
            "total": scan_error_total,
            "items": [
                _evidence(
                    f"Скан #{event.id} · волна #{event.batch_id}",
                    subtitle=event.get_stage_display(),
                    facts=(
                        f"Получено: {_short(event.scan_value, 100) or '—'}",
                        f"Ожидалось: {_short(event.expected_value, 100) or '—'}",
                        f"Оператор: {event.created_by or '—'}",
                        f"Время: {_format_dt(event.created_at)}",
                    ),
                    message=event.message or "Причина ошибки сканирования не записана.",
                )
                for event in scan_errors_qs.order_by("-created_at", "-id")[:EVIDENCE_LIMIT]
            ],
        }

    invalid_stock_qs = FbsStockBalance.objects.filter(
        qty__lt=F("available_qty") + F("reserved_qty")
    ).select_related("agency", "box")
    invalid_stock_total = invalid_stock_qs.count()
    if invalid_stock_total:
        result["stock"] = {
            "total": invalid_stock_total,
            "items": [
                _evidence(
                    f"Остаток FBS #{row.id} · {row.agency}",
                    subtitle=f"SKU {row.sku_code or '—'} · короб {row.box.box_code}",
                    facts=(
                        f"Факт: {row.qty}",
                        f"Доступно: {row.available_qty}",
                        f"Резерв: {row.reserved_qty}",
                        f"Дефицит покрытия: {row.available_qty + row.reserved_qty - row.qty}",
                        f"Обновлено: {_format_dt(row.updated_at)}",
                    ),
                )
                for row in invalid_stock_qs.order_by("id")[:EVIDENCE_LIMIT]
            ],
        }
    return result


def _incidents_from_fbs_report(report: dict) -> list[InspectionIncident]:
    incidents: list[InspectionIncident] = []
    evidence_by_code = report.get("inspection_evidence") or {}
    for check in report.get("checks") or []:
        source_severity = str(check.get("severity") or "ok")
        if source_severity == "ok":
            continue
        code = str(check.get("code") or "unknown")
        severity = (
            SEVERITY_CRITICAL
            if source_severity == "blocked"
            else SEVERITY_WARNING
        )
        evidence_payload = evidence_by_code.get(code) or {}
        incidents.append(
            _incident(
                code=f"fbs_{code}",
                severity=severity,
                contour="FBS",
                title=str(check.get("title") or "Проверка FBS"),
                message=str(check.get("message") or "Требуется проверка FBS."),
                recommendation=FBS_RECOMMENDATIONS.get(
                    code,
                    "Проверить вручную. Изменения складского процесса запрещены.",
                ),
                source="fbs_readiness",
                count=int(check.get("count") or 0),
                why_it_matters=FBS_IMPACTS.get(code, "Сбой FBS может остановить оператора или нарушить обмен данными."),
                check_rule=FBS_RULES.get(code, "Проверка основана на штатном read-only отчёте готовности FBS."),
                evidence=evidence_payload.get("items") or (),
                evidence_total=evidence_payload.get("total") or 0,
            )
        )
    return incidents


def _collect_fbs(now: datetime) -> dict:
    from fbs.readiness import build_fbs_readiness_report

    report = build_fbs_readiness_report(now=now)
    report["inspection_evidence"] = _build_fbs_evidence(report, now)
    return {
        "incidents": _incidents_from_fbs_report(report),
        "metrics": {
            "scan_total": int(report.get("scan_total") or 0),
            "scan_errors": int(report.get("scan_errors") or 0),
            "scan_error_rate": report.get("scan_error_rate") or 0,
            "active_batches": int(report.get("active_batches") or 0),
            "stale_batches": int(report.get("stale_batches") or 0),
        },
    }


def _collect_equipment(now: datetime) -> dict:
    from agent.models import AgentCommand, AgentEvent, DeviceAgent

    day_ago = now - timedelta(hours=24)
    recently_used_at = now - timedelta(days=7)
    offline_at = now - timedelta(minutes=5)
    recent_agents = DeviceAgent.objects.filter(last_seen__gte=recently_used_at)
    total_recent = recent_agents.count()
    offline_agents = list(
        recent_agents.filter(last_seen__lt=offline_at).order_by("last_seen", "id")[:EVIDENCE_LIMIT]
    )
    offline_recent = recent_agents.filter(last_seen__lt=offline_at).count()
    failed_commands_qs = AgentCommand.objects.filter(
        status=AgentCommand.STATUS_FAILED,
        updated_at__gte=day_ago,
    )
    failed_commands = failed_commands_qs.count()
    error_events_qs = AgentEvent.objects.filter(
        event_type=AgentEvent.EVENT_ERROR,
        created_at__gte=day_ago,
    )
    error_events = error_events_qs.count()

    incidents: list[InspectionIncident] = []
    if offline_recent:
        incidents.append(
            _incident(
                code="equipment_agents_offline",
                severity=SEVERITY_WARNING,
                contour="Оборудование",
                title="Недавние Desktop‑агенты не на связи",
                message=(
                    f"Не отвечают более пяти минут: {offline_recent} "
                    f"из {total_recent} недавно использованных агентов."
                ),
                recommendation="Проверить компьютер, сеть, Desktop Agent, сканер и принтер.",
                source="device_agents",
                count=offline_recent,
                why_it_matters="Недоступный агент не получает команды печати и не передаёт события рабочего места.",
                check_rule="Агент, использованный за последние 7 дней, считается не на связи после 5 минут без heartbeat.",
                evidence=(
                    _evidence(
                        f"Агент {agent.name or agent.host or agent.agent_id}",
                        subtitle=f"ID: {agent.agent_id}",
                        facts=(
                            f"Хост: {agent.host or '—'}",
                            f"Версия: {agent.version or '—'}",
                            f"Последняя связь: {_format_dt(agent.last_seen)} ({_age_text(now, agent.last_seen)} назад)",
                        ),
                        message="Heartbeat не поступал дольше допустимых пяти минут.",
                    )
                    for agent in offline_agents
                ),
                evidence_total=offline_recent,
            )
        )
    if failed_commands or error_events:
        error_evidence: list[dict] = []
        for command in failed_commands_qs.order_by("-updated_at", "-id")[:EVIDENCE_LIMIT]:
            error_evidence.append(
                _evidence(
                    f"Команда агента #{command.id} · {command.command}",
                    subtitle=f"Агент: {command.agent_id or 'не назначен'}",
                    facts=(
                        f"Статус: {command.get_status_display()}",
                        f"Создана: {_format_dt(command.created_at)}",
                        f"Обновлена: {_format_dt(command.updated_at)}",
                    ),
                    message=command.error or "Текст ошибки команды отсутствует.",
                )
            )
        remaining = EVIDENCE_LIMIT - len(error_evidence)
        if remaining > 0:
            for event in error_events_qs.order_by("-created_at", "-id")[:remaining]:
                error_evidence.append(
                    _evidence(
                        f"Событие агента #{event.id}",
                        subtitle=f"Агент: {event.agent_id or 'не указан'}",
                        facts=(
                            f"Заявка: {event.context_order_id or '—'}",
                            f"Короб: {event.context_box_id or '—'}",
                            f"Роль: {event.context_role or '—'}",
                            f"Время: {_format_dt(event.created_at)}",
                        ),
                        message=_payload_message(event.payload) or "Агент передал событие типа error без текстового описания.",
                    )
                )
        incidents.append(
            _incident(
                code="equipment_recent_errors",
                severity=SEVERITY_WARNING,
                contour="Оборудование",
                title="Ошибки команд или событий оборудования",
                message=(
                    f"За 24 часа: неуспешных команд — {failed_commands}, "
                    f"событий ошибки — {error_events}."
                ),
                recommendation="Открыть журнал агента и проверить конкретное рабочее место.",
                source="device_agents",
                count=failed_commands + error_events,
                why_it_matters="Ошибка агента может означать непринятый скан, неполученную команду или неподтверждённую печать.",
                check_rule="В выборку входят failed-команды и события error, созданные за последние 24 часа.",
                evidence=error_evidence,
                evidence_total=failed_commands + error_events,
            )
        )
    return {
        "incidents": incidents,
        "metrics": {
            "recent_agents": total_recent,
            "offline_agents": offline_recent,
            "failed_commands_24h": failed_commands,
            "error_events_24h": error_events,
        },
    }


def _collect_printing(now: datetime) -> dict:
    from processing_app.models import ProcessingPrintJob

    day_ago = now - timedelta(hours=24)
    stale_printing_at = now - timedelta(minutes=5)
    stale_pending_at = now - timedelta(hours=1)
    recent_window = now - timedelta(days=7)

    failed_qs = ProcessingPrintJob.objects.filter(
        status=ProcessingPrintJob.STATUS_FAILED,
        updated_at__gte=day_ago,
    )
    stale_printing_qs = ProcessingPrintJob.objects.filter(
        status=ProcessingPrintJob.STATUS_PRINTING,
        updated_at__lt=stale_printing_at,
    )
    stale_pending_qs = ProcessingPrintJob.objects.filter(
        status=ProcessingPrintJob.STATUS_PENDING,
        created_at__gte=recent_window,
        created_at__lt=stale_pending_at,
    )
    failed = failed_qs.count()
    stale_printing = stale_printing_qs.count()
    stale_pending = stale_pending_qs.count()

    incidents: list[InspectionIncident] = []
    if stale_printing:
        incidents.append(
            _incident(
                code="printing_stale_lease",
                severity=SEVERITY_CRITICAL,
                contour="Печать",
                title="Задания печати зависли в работе",
                message=f"Заданий в printing без обновления более пяти минут: {stale_printing}.",
                recommendation=(
                    "Проверить факт печати и lease. Переотдавать задание только после "
                    "подтверждения оператора."
                ),
                source="processing_print_jobs",
                count=stale_printing,
                why_it_matters="Повторная печать без сверки может создать дубли этикеток, а ожидание блокирует оператора.",
                check_rule="Статус printing без обновления более 5 минут считается зависшим lease.",
                evidence=(
                    _evidence(
                        f"Задание печати #{job.id} · заявка {job.order_id or '—'}",
                        subtitle=f"Штрихкод: {job.barcode or '—'}",
                        facts=(
                            f"Агент: {job.agent or 'не назначен'}",
                            f"Принтер: {job.printer_name or 'не указан'}",
                            f"Запросил: {job.requested_by or '—'}",
                            f"Попыток: {getattr(job, 'attempt_count', 0)}",
                            f"Lease до: {_format_dt(getattr(job, 'lease_until', None))}",
                            f"Обновлено: {_format_dt(job.updated_at)} ({_age_text(now, job.updated_at)} назад)",
                        ),
                        message=job.error or "Задание осталось в printing без текста ошибки.",
                    )
                    for job in stale_printing_qs.order_by("updated_at", "id")[:EVIDENCE_LIMIT]
                ),
                evidence_total=stale_printing,
            )
        )
    if failed or stale_pending:
        queue_evidence: list[dict] = []
        for job in failed_qs.order_by("-updated_at", "-id")[:EVIDENCE_LIMIT]:
            queue_evidence.append(
                _evidence(
                    f"Задание печати #{job.id} · заявка {job.order_id or '—'}",
                    subtitle=f"Ошибка · штрихкод {job.barcode or '—'}",
                    facts=(
                        f"Агент: {job.agent or 'не назначен'}",
                        f"Принтер: {job.printer_name or 'не указан'}",
                        f"Попыток: {getattr(job, 'attempt_count', 0)}",
                        f"Обновлено: {_format_dt(job.updated_at)}",
                    ),
                    message=job.error or "Статус failed установлен без текста ошибки.",
                )
            )
        remaining = EVIDENCE_LIMIT - len(queue_evidence)
        if remaining > 0:
            for job in stale_pending_qs.order_by("created_at", "id")[:remaining]:
                queue_evidence.append(
                    _evidence(
                        f"Задание печати #{job.id} · заявка {job.order_id or '—'}",
                        subtitle=f"Ожидает · штрихкод {job.barcode or '—'}",
                        facts=(
                            f"Агент: {job.agent or 'не назначен'}",
                            f"Принтер: {job.printer_name or 'не указан'}",
                            f"Создано: {_format_dt(job.created_at)} ({_age_text(now, job.created_at)} назад)",
                        ),
                        message="Задание ожидает выполнения более одного часа.",
                    )
                )
        incidents.append(
            _incident(
                code="printing_queue_problems",
                severity=SEVERITY_WARNING,
                contour="Печать",
                title="Очередь печати требует внимания",
                message=(
                    f"Ошибок за 24 часа: {failed}; ожидают более часа "
                    f"среди заданий последних семи суток: {stale_pending}."
                ),
                recommendation="Проверить назначенный агент и принтер. Повторять печать только после сверки.",
                source="processing_print_jobs",
                count=failed + stale_pending,
                why_it_matters="Этикетка могла не напечататься, поэтому заявка или товар не могут безопасно перейти дальше.",
                check_rule="В выборку входят failed за 24 часа и pending старше часа среди заданий последних 7 дней.",
                evidence=queue_evidence,
                evidence_total=failed + stale_pending,
            )
        )
    return {
        "incidents": incidents,
        "metrics": {
            "failed_24h": failed,
            "stale_printing": stale_printing,
            "stale_pending": stale_pending,
        },
    }


def _collect_billing(_now: datetime) -> dict:
    from billing.models import StorageBillingError

    unresolved = StorageBillingError.objects.filter(resolved_at__isnull=True)
    error_count = unresolved.filter(
        severity=StorageBillingError.SEVERITY_ERROR
    ).count()
    warning_count = unresolved.filter(
        severity=StorageBillingError.SEVERITY_WARN
    ).count()
    unresolved_evidence = [
        _evidence(
            f"Ошибка биллинга #{item.id} · {item.client}",
            subtitle=f"{item.get_error_type_display()} · {item.get_severity_display()}",
            facts=(
                f"Расчётный день: {item.day or '—'}",
                f"Storage day: #{item.storage_day_id or '—'}",
                f"SKU: {item.sku_code or '—'}",
                f"Палета: {item.pallet_code or '—'}",
                f"Короб: {item.box_code or '—'}",
                f"Создано: {_format_dt(item.created_at)}",
            ),
            message=item.message or "Текст ошибки биллинга отсутствует.",
        )
        for item in unresolved.select_related("client").order_by("-created_at", "-id")[:EVIDENCE_LIMIT]
    ]
    incidents: list[InspectionIncident] = []
    if error_count or warning_count:
        incidents.append(
            _incident(
                code="billing_unresolved",
                severity=(
                    SEVERITY_CRITICAL if error_count else SEVERITY_WARNING
                ),
                contour="Биллинг",
                title="Неразобранные ошибки хранения",
                message=(
                    f"Критичных: {error_count}; предупреждений: {warning_count}."
                ),
                recommendation="Разобрать источник данных и тариф. Начисления автоматически не корректировать.",
                source="storage_billing_errors",
                count=error_count + warning_count,
                why_it_matters="Ошибка может привести к неверному начислению хранения или к пропуску начисления клиенту.",
                check_rule="Показываются StorageBillingError без resolved_at; critical соответствует severity=error.",
                evidence=unresolved_evidence,
                evidence_total=error_count + warning_count,
            )
        )
    return {
        "incidents": incidents,
        "metrics": {
            "unresolved_errors": error_count,
            "unresolved_warnings": warning_count,
        },
    }


def _collect_warehouse_guard(_now: datetime) -> dict:
    from django.db.models import F
    from sklad.models import WarehouseStockSnapshot

    invalid_qs = WarehouseStockSnapshot.objects.filter(
        is_archived=False,
        qty__lt=F("available_qty"),
    ).select_related("agency", "location", "container")
    invalid = invalid_qs.count()
    incidents: list[InspectionIncident] = []
    if invalid:
        incidents.append(
            _incident(
                code="warehouse_qty_below_available",
                severity=SEVERITY_CRITICAL,
                contour="Складской контроль",
                title="Доступный остаток превышает фактический",
                message=f"Строк с qty меньше available_qty: {invalid}.",
                recommendation=(
                    "Ничего не исправлять автоматически. Передать программисту и "
                    "начальнику склада для адресной сверки."
                ),
                source="warehouse_stock_snapshot",
                count=invalid,
                why_it_matters="Система показывает доступным больше товара, чем физически числится; дальнейший резерв может усилить расхождение.",
                check_rule="Для каждой неархивной строки WarehouseStockSnapshot должно выполняться qty ≥ available_qty.",
                evidence=(
                    _evidence(
                        f"Снимок #{row.id} · {row.agency}",
                        subtitle=f"SKU {row.sku_code or '—'} · {row.name or 'без названия'}",
                        facts=(
                            f"Штрихкод: {row.barcode or '—'}",
                            f"Контейнер: {row.container_code or row.container or '—'}",
                            f"Зона/место: {row.zone_code or '—'} / {row.location or '—'}",
                            f"Факт: {row.qty}; доступно: {row.available_qty}; превышение: {row.available_qty - row.qty}",
                            f"Резервы: обработка {row.processing_reserved_qty}, отгрузка {row.shipping_reserved_qty}, прочие {row.other_reserved_qty}",
                            f"Контекст: {row.source_context_type or '—'} #{row.source_context_id or '—'}",
                            f"Обновлено: {_format_dt(row.updated_at)}",
                        ),
                        message="Защитный инвариант qty ≥ available_qty нарушен.",
                    )
                    for row in invalid_qs.order_by("id")[:EVIDENCE_LIMIT]
                ),
                evidence_total=invalid,
            )
        )
    return {
        "incidents": incidents,
        "metrics": {"invalid_snapshots": invalid},
    }


def _collect_processing_deep(_now: datetime) -> dict:
    from processing_app.order_audit import audit_processing_orders

    report = audit_processing_orders(
        limit=100,
        include_done=False,
        only_issues=True,
    )
    incidents: list[InspectionIncident] = []
    if report.issue_count:
        incidents.append(
            _incident(
                code="processing_live_issues",
                severity=SEVERITY_WARNING,
                contour="Обработка",
                title="Активные заявки обработки требуют проверки",
                message=(
                    f"Проверено заявок: {report.scanned_count}; "
                    f"проблемных в выборке: {report.issue_count}."
                ),
                recommendation="Открыть read-only аудит обработки и проверить блокеры по заявкам.",
                source="processing_order_audit",
                count=report.issue_count,
                why_it_matters="Активная заявка может быть заблокирована несогласованным состоянием этапов обработки.",
                check_rule="Read-only аудит проверяет до 100 незавершённых заявок и показывает только найденные проблемы.",
            )
        )
    return {
        "incidents": incidents,
        "metrics": {
            "scanned": report.scanned_count,
            "issues": report.issue_count,
            "sample_truncated": report.sample_truncated,
        },
    }


DEFAULT_SOURCE_BUILDERS: tuple[tuple[str, str, Callable[[datetime], dict]], ...] = (
    ("fbs", "Контроль FBS", _collect_fbs),
    ("equipment", "Оборудование", _collect_equipment),
    ("printing", "Очередь печати", _collect_printing),
    ("billing", "Ошибки биллинга", _collect_billing),
    ("warehouse_guard", "Защитная проверка склада", _collect_warehouse_guard),
)
DEEP_SOURCE_BUILDERS: tuple[tuple[str, str, Callable[[datetime], dict]], ...] = (
    ("processing", "Аудит активной обработки", _collect_processing_deep),
)


def _source_failure_incident(code: str, title: str, exc: Exception) -> InspectionIncident:
    return _incident(
        code=f"source_{code}_unavailable",
        severity=SEVERITY_WARNING,
        contour="Самоконтроль инспектора",
        title=f"Источник «{title}» временно недоступен",
        message=f"Проверка не выполнена ({type(exc).__name__}). Рабочие процессы не затронуты.",
        recommendation="Передать программисту диагностику источника. Повторить только read-only проверку.",
        source=code,
        count=1,
        why_it_matters="Часть диагностики отсутствует, поэтому общий отчёт может быть неполным.",
        check_rule="Ошибка изолирована внутри одного источника; остальные проверки продолжают выполняться.",
    )


def build_ai_inspection_report(
    *,
    now: datetime | None = None,
    deep: bool = False,
    source_builders: Iterable[
        tuple[str, str, Callable[[datetime], dict]]
    ]
    | None = None,
) -> dict:
    """Build a fail-safe report without changing WMS or external systems."""

    generated_at = now or timezone.now()
    builders = list(source_builders or DEFAULT_SOURCE_BUILDERS)
    if source_builders is None and deep:
        builders.extend(DEEP_SOURCE_BUILDERS)

    incidents: list[InspectionIncident] = []
    sources: list[dict] = []
    metrics: dict[str, dict] = {}
    for code, title, collector in builders:
        started_at = perf_counter()
        try:
            payload = collector(generated_at) or {}
            source_incidents = list(payload.get("incidents") or [])
            incidents.extend(source_incidents)
            metrics[code] = dict(payload.get("metrics") or {})
            sources.append(
                {
                    "code": code,
                    "title": title,
                    "status": "attention" if source_incidents else "ok",
                    "incident_count": len(source_incidents),
                    "duration_ms": round((perf_counter() - started_at) * 1000),
                    "message": (
                        "Есть сигналы для проверки."
                        if source_incidents
                        else "Проблем не найдено."
                    ),
                }
            )
        except Exception as exc:  # noqa: BLE001 - fail-safe boundary per source
            incidents.append(_source_failure_incident(code, title, exc))
            sources.append(
                {
                    "code": code,
                    "title": title,
                    "status": "unavailable",
                    "incident_count": 1,
                    "duration_ms": round((perf_counter() - started_at) * 1000),
                    "message": f"Не выполнено: {type(exc).__name__}.",
                }
            )

    incidents.sort(
        key=lambda item: (
            SEVERITY_ORDER.get(item.severity, 99),
            item.contour.casefold(),
            item.title.casefold(),
        )
    )
    critical_count = sum(
        1 for item in incidents if item.severity == SEVERITY_CRITICAL
    )
    warning_count = sum(
        1 for item in incidents if item.severity == SEVERITY_WARNING
    )
    info_count = sum(1 for item in incidents if item.severity == SEVERITY_INFO)
    unavailable_sources = sum(
        1 for item in sources if item.get("status") == "unavailable"
    )
    overall = (
        SEVERITY_CRITICAL
        if critical_count
        else SEVERITY_WARNING
        if warning_count or unavailable_sources
        else "ok"
    )
    return {
        "read_only": True,
        "automatic_corrections": False,
        "deep": bool(deep),
        "generated_at": generated_at,
        "generated_at_iso": generated_at.isoformat(),
        "overall": overall,
        "summary": {
            "total": len(incidents),
            "critical": critical_count,
            "warning": warning_count,
            "info": info_count,
            "sources": len(sources),
            "healthy_sources": sum(1 for item in sources if item["status"] == "ok"),
            "attention_sources": sum(
                1 for item in sources if item["status"] == "attention"
            ),
            "unavailable_sources": unavailable_sources,
        },
        "incidents": [item.as_dict() for item in incidents],
        "sources": sources,
        "metrics": metrics,
    }
