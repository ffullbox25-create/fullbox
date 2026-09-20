from datetime import timedelta

from django.db.models import Count, F, Max, Q, Sum
from django.utils import timezone

from .flags import feature_snapshot
from .integrations.http import marketplace_credentials_status
from .models import (
    FbsIntegrationProfile,
    FbsMarketplaceCommand,
    FbsOrderLabel,
    FbsPickBatch,
    FbsPickScanEvent,
    FbsPickingCart,
    FbsStockBalance,
    FbsSyncCursor,
    FbsToteBinding,
    FbsWorkstation,
)


SEVERITY_ORDER = {"ok": 0, "warning": 1, "blocked": 2}

SCAN_ERROR_ACTIONS = {
    "Скан не совпадает с этикеткой этого заказа.": (
        "Отсканируйте QR текущего заказа. ШК товара, КИЗ и этикетка предыдущего заказа не подходят."
    ),
    "ЧЗ уже отобран или использован в другом заказе.": (
        "Отложите КИЗ в служебную тару и возьмите другой КИЗ этого товара."
    ),
    "Отсканирован штрихкод товара вместо КИЗа. Отсканируйте Честный знак повторно.": (
        "После ШК товара сканируйте только Data Matrix Честного знака."
    ),
    "Неверная длина КИЗа. Отсканируйте Честный знак повторно.": (
        "Повторно отсканируйте полный Data Matrix без ручного ввода."
    ),
    "Скан ячейки не совпадает с физическим адресом маршрута.": (
        "Сверьте адрес на экране и QR физической ячейки."
    ),
    "Штрихкод товара не совпадает с заданием.": (
        "Сверьте товар с заданием и отсканируйте его ШК повторно."
    ),
    "Скан короба FBS не совпадает с заданием.": (
        "Сверьте номер короба на экране и отсканируйте QR нужного короба."
    ),
}


def _check(code, title, severity, message, *, count=None):
    return {
        "code": code,
        "title": title,
        "severity": severity,
        "message": message,
        "count": count,
    }


def _overall_status(checks):
    severity = max(
        (str(check.get("severity") or "ok") for check in checks),
        key=lambda value: SEVERITY_ORDER.get(value, 0),
        default="ok",
    )
    return {
        "severity": severity,
        "label": {
            "ok": "Готово к работе",
            "warning": "Требует внимания",
            "blocked": "Запуск заблокирован",
        }[severity],
    }


def build_fbs_readiness_report(*, now=None):
    """Build a read-only pre-shift report. No marketplace requests or writes."""
    now = now or timezone.now()
    day_ago = now - timedelta(hours=24)
    stale_sync_at = now - timedelta(minutes=5)
    stale_work_at = now - timedelta(hours=1)
    flags = feature_snapshot()
    checks = []

    if not flags["module"]:
        checks.append(
            _check("module", "Модуль FBS", "blocked", "Модуль FBS выключен.")
        )
    else:
        checks.append(_check("module", "Модуль FBS", "ok", "Модуль включен."))

    if not flags["warehouse_writes"]:
        checks.append(
            _check(
                "warehouse_writes",
                "Складские операции FBS",
                "blocked",
                "Складские операции FBS выключены.",
            )
        )
    else:
        checks.append(
            _check(
                "warehouse_writes",
                "Складские операции FBS",
                "ok",
                "Складские операции включены.",
            )
        )

    profiles = list(
        FbsIntegrationProfile.objects.filter(is_active=True)
        .select_related("agency")
        .order_by("agency__agn_name", "marketplace", "id")
    )
    cursors = {
        cursor.profile_id: cursor
        for cursor in FbsSyncCursor.objects.filter(
            profile_id__in=[profile.id for profile in profiles],
            stream=FbsSyncCursor.STREAM_ORDERS,
            cursor_key="default",
        )
    }
    profile_rows = []
    profile_blocked = 0
    profile_warning = 0
    for profile in profiles:
        credentials = marketplace_credentials_status(profile)
        cursor = cursors.get(profile.id)
        reasons = []
        severity = "ok"
        if not credentials.get("configured"):
            severity = "blocked"
            reasons.append(str(credentials.get("error") or "нет API-реквизитов"))
        if not str(profile.external_warehouse_id or "").strip():
            severity = "blocked"
            reasons.append("не выбран склад маркетплейса")
        if not profile.order_pull_enabled:
            severity = max((severity, "warning"), key=lambda value: SEVERITY_ORDER[value])
            reasons.append("получение заказов выключено")
        if cursor and cursor.last_error:
            severity = max((severity, "warning"), key=lambda value: SEVERITY_ORDER[value])
            reasons.append(str(cursor.last_error)[:180])
        elif (
            profile.order_pull_enabled
            and cursor
            and cursor.last_success_at
            and cursor.last_success_at < stale_sync_at
        ):
            severity = max((severity, "warning"), key=lambda value: SEVERITY_ORDER[value])
            reasons.append("заказы не обновлялись более 5 минут")
        elif profile.order_pull_enabled and not cursor:
            severity = max((severity, "warning"), key=lambda value: SEVERITY_ORDER[value])
            reasons.append("синхронизация заказов еще не запускалась")

        if severity == "blocked":
            profile_blocked += 1
        elif severity == "warning":
            profile_warning += 1
        profile_rows.append(
            {
                "id": profile.id,
                "agency": profile.agency,
                "marketplace": profile.get_marketplace_display(),
                "warehouse": profile.external_warehouse_id or "-",
                "severity": severity,
                "message": "; ".join(reasons) or "Подключение готово",
                "last_success_at": cursor.last_success_at if cursor else None,
                "last_polled_at": cursor.last_polled_at if cursor else None,
                "order_pull_enabled": profile.order_pull_enabled,
                "outbox_enabled": profile.outbox_enabled,
                "marking_push_enabled": profile.marking_push_enabled,
                "stock_push_enabled": profile.stock_push_enabled,
            }
        )

    if not profiles:
        checks.append(
            _check(
                "profiles",
                "Подключения клиентов",
                "blocked",
                "Нет активных профилей WB/Ozon.",
                count=0,
            )
        )
    elif profile_blocked:
        checks.append(
            _check(
                "profiles",
                "Подключения клиентов",
                "blocked",
                f"Не готовы профили: {profile_blocked}.",
                count=len(profiles),
            )
        )
    elif profile_warning:
        checks.append(
            _check(
                "profiles",
                "Подключения клиентов",
                "warning",
                f"Требуют внимания профили: {profile_warning}.",
                count=len(profiles),
            )
        )
    else:
        checks.append(
            _check(
                "profiles",
                "Подключения клиентов",
                "ok",
                "Все активные профили готовы.",
                count=len(profiles),
            )
        )

    workstations = FbsWorkstation.objects.filter(is_active=True).count()
    if workstations:
        checks.append(
            _check(
                "workstations",
                "Рабочие места",
                "ok",
                "Активные рабочие места созданы.",
                count=workstations,
            )
        )
    else:
        checks.append(
            _check(
                "workstations",
                "Рабочие места",
                "blocked",
                "Нет активных рабочих мест FBS.",
                count=0,
            )
        )

    active_totes = FbsPickingCart.objects.filter(is_active=True).count()
    bound_totes = FbsToteBinding.objects.filter(tote__is_active=True).count()
    tote_states = {
        row["state"]: row["total"]
        for row in FbsToteBinding.objects.filter(tote__is_active=True)
        .values("state")
        .annotate(total=Count("id"))
    }
    free_totes = tote_states.get(FbsToteBinding.STATE_FREE, 0)
    unbound_totes = max(active_totes - bound_totes, 0) + tote_states.get(
        FbsToteBinding.STATE_UNBOUND, 0
    )
    if not active_totes:
        tote_severity = "blocked"
        tote_message = "Нет активной тары FBS."
    elif not free_totes:
        tote_severity = "warning"
        tote_message = "Нет тары в свободном фонде для новой волны."
    elif unbound_totes:
        tote_severity = "warning"
        tote_message = f"Тара без корректной привязки: {unbound_totes}."
    else:
        tote_severity = "ok"
        tote_message = "Свободная тара доступна."
    checks.append(
        _check("totes", "Тара FBS", tote_severity, tote_message, count=active_totes)
    )

    stale_batches = FbsPickBatch.objects.filter(
        status__in=(
            FbsPickBatch.STATUS_IN_PROGRESS,
            FbsPickBatch.STATUS_VERIFICATION,
        ),
        updated_at__lt=stale_work_at,
    ).count()
    active_batches = FbsPickBatch.objects.filter(
        status__in=(
            FbsPickBatch.STATUS_QUEUED,
            FbsPickBatch.STATUS_IN_PROGRESS,
            FbsPickBatch.STATUS_VERIFICATION,
        )
    ).count()
    checks.append(
        _check(
            "waves",
            "Активные волны",
            "warning" if stale_batches else "ok",
            (
                f"Без обновления более часа: {stale_batches}."
                if stale_batches
                else "Зависших активных волн не найдено."
            ),
            count=active_batches,
        )
    )

    failed_commands = FbsMarketplaceCommand.objects.filter(
        status__in=(
            FbsMarketplaceCommand.STATUS_FAILED,
            FbsMarketplaceCommand.STATUS_CONFLICT,
        ),
        updated_at__gte=day_ago,
    ).count()
    stuck_commands = FbsMarketplaceCommand.objects.filter(
        status__in=(
            FbsMarketplaceCommand.STATUS_PENDING,
            FbsMarketplaceCommand.STATUS_SENT,
            FbsMarketplaceCommand.STATUS_RETRY,
        ),
        updated_at__lt=stale_work_at,
    ).count()
    command_problems = failed_commands + stuck_commands
    checks.append(
        _check(
            "outbox",
            "Обмен с маркетплейсами",
            "warning" if command_problems else "ok",
            (
                f"Ошибки за 24 часа: {failed_commands}; зависшие команды: {stuck_commands}."
                if command_problems
                else "Ошибок и зависших команд не найдено."
            ),
            count=command_problems,
        )
    )

    label_errors = FbsOrderLabel.objects.filter(
        status=FbsOrderLabel.STATUS_ERROR,
        updated_at__gte=day_ago,
    ).count()
    stale_labels = FbsOrderLabel.objects.filter(
        status=FbsOrderLabel.STATUS_REQUESTED,
        requested_at__lt=stale_work_at,
    ).count()
    checks.append(
        _check(
            "labels",
            "Этикетки заказов",
            "warning" if label_errors or stale_labels else "ok",
            (
                f"Ошибки за 24 часа: {label_errors}; ожидают более часа: {stale_labels}."
                if label_errors or stale_labels
                else "Очередь этикеток без ошибок."
            ),
            count=label_errors + stale_labels,
        )
    )

    scan_totals = FbsPickScanEvent.objects.filter(created_at__gte=day_ago).aggregate(
        total=Count("id"),
        errors=Count("id", filter=Q(result=FbsPickScanEvent.RESULT_ERROR)),
    )
    scan_total = int(scan_totals["total"] or 0)
    scan_errors = int(scan_totals["errors"] or 0)
    scan_error_rate = round((scan_errors * 100 / scan_total), 1) if scan_total else 0
    scan_stage_rows = list(
        FbsPickScanEvent.objects.filter(created_at__gte=day_ago)
        .values("stage")
        .annotate(
            total=Count("id"),
            errors=Count("id", filter=Q(result=FbsPickScanEvent.RESULT_ERROR)),
        )
        .order_by("stage")
    )
    stage_labels = dict(FbsPickScanEvent.STAGE_CHOICES)
    for row in scan_stage_rows:
        row["label"] = stage_labels.get(row["stage"], row["stage"])
        row["error_rate"] = (
            round(int(row["errors"] or 0) * 100 / int(row["total"] or 1), 1)
        )
    scan_error_reason_rows = list(
        FbsPickScanEvent.objects.filter(
            created_at__gte=day_ago,
            result=FbsPickScanEvent.RESULT_ERROR,
        )
        .values("stage", "message")
        .annotate(total=Count("id"), last_at=Max("created_at"))
        .order_by("-total", "-last_at", "stage", "message")
    )
    for row in scan_error_reason_rows:
        row["stage_label"] = stage_labels.get(row["stage"], row["stage"])
        row["action"] = SCAN_ERROR_ACTIONS.get(
            str(row["message"] or ""),
            "Повторите сканирование по текущей подсказке на экране.",
        )
    top_scan_reason = scan_error_reason_rows[0] if scan_error_reason_rows else None
    scan_message = (
        f"За 24 часа безопасно отклонено {scan_errors} из {scan_total} сканов "
        f"({scan_error_rate}%)."
    )
    if top_scan_reason:
        scan_message += (
            f" Чаще всего: {top_scan_reason['message']} "
            f"({top_scan_reason['total']})."
        )
    checks.append(
        _check(
            "scans",
            "Отклоненные сканы",
            "warning" if scan_total >= 5 and scan_error_rate > 20 else "ok",
            scan_message,
            count=scan_errors,
        )
    )

    stock_totals = FbsStockBalance.objects.aggregate(
        qty=Sum("qty"),
        available=Sum("available_qty"),
        reserved=Sum("reserved_qty"),
    )
    invalid_stock = FbsStockBalance.objects.filter(
        qty__lt=F("available_qty") + F("reserved_qty")
    ).count()
    checks.append(
        _check(
            "stock",
            "Целостность FBS-остатка",
            "blocked" if invalid_stock else "ok",
            (
                f"Нарушена формула остатка в строках: {invalid_stock}."
                if invalid_stock
                else "Факт покрывает доступный остаток и резерв."
            ),
            count=invalid_stock,
        )
    )

    report = {
        "generated_at": now,
        "flags": flags,
        "checks": checks,
        "profiles": profile_rows,
        "profile_count": len(profiles),
        "workstation_count": workstations,
        "active_totes": active_totes,
        "free_totes": free_totes,
        "unbound_totes": unbound_totes,
        "tote_states": tote_states,
        "active_batches": active_batches,
        "stale_batches": stale_batches,
        "command_problems": command_problems,
        "label_problems": label_errors + stale_labels,
        "scan_total": scan_total,
        "scan_errors": scan_errors,
        "scan_error_rate": scan_error_rate,
        "scan_stage_rows": scan_stage_rows,
        "scan_error_reason_rows": scan_error_reason_rows,
        "stock": {
            "qty": int(stock_totals["qty"] or 0),
            "available": int(stock_totals["available"] or 0),
            "reserved": int(stock_totals["reserved"] or 0),
            "invalid": invalid_stock,
        },
    }
    report["overall"] = _overall_status(checks)
    return report


def add_equipment_readiness(report, equipment_rows):
    """Merge already calculated agent/printer/scanner states into a report."""
    planned_rows = [row for row in equipment_rows if row.get("planned", True)]
    total = len(planned_rows)
    ready = sum(1 for row in planned_rows if row.get("ready"))
    if not total:
        severity = "blocked"
        message = "Не открыто ни одного рабочего места FBS для текущей смены."
    elif not ready:
        severity = "blocked"
        message = "Ни одно рабочее место не готово: проверьте Agent, принтер и сканер."
    elif ready < total:
        severity = "warning"
        message = f"Готово рабочих мест: {ready} из {total}."
    else:
        severity = "ok"
        message = f"Все рабочие места готовы: {ready}."
    report["equipment_rows"] = equipment_rows
    report["ready_workstations"] = ready
    report["planned_workstations"] = total
    report["closed_workstations"] = len(equipment_rows) - total
    report["checks"] = [
        check for check in report["checks"] if check.get("code") != "workstations"
    ]
    report["checks"].append(
        _check("workstations", "Оборудование рабочих мест", severity, message, count=ready)
    )
    report["overall"] = _overall_status(report["checks"])
    return report
