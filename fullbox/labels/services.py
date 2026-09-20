from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models
from django.http import FileResponse, HttpResponseBadRequest, JsonResponse
from django.utils import timezone

from agent.models import AgentCommand, AgentEvent, DeviceAgent
from processing_app.models import ProcessingPrintJob

from .utils import (
    LABEL_FIELDS,
    LABEL_SIZES,
    LABEL_TEMPLATE_KEYS,
    LABEL_TEMPLATES,
    agent_printer_details_from_meta,
    agent_printer_names_from_meta,
    clean_label_dimensions,
    clean_label_enabled,
    get_effective_label_template,
    load_available_printers_data,
    load_label_settings,
    load_processing_param_template_bindings,
    load_print_agent_status,
    load_scanner_settings,
    processing_param_template_options,
    resolve_processing_param_template_key,
    normalize_scanner_settings,
    save_label_settings,
    save_processing_param_template_bindings,
    save_scanner_settings,
    split_printers_by_kind,
    synchronize_product_label_settings,
)


AGENT_VERSION = "1.0.66"
ALLOWED_ROLES = (
    "admin",
    "director",
    "accountant",
    "head_manager",
    "processing_head",
    "processing_worker",
    "manager",
    "storekeeper",
    "logistician",
    "reachtruck_driver",
    "picker",
    "developer",
)
PROCESSING_PARAM_TEMPLATE_FIELDS = (
    {"label_key": "item", "label": "Маркировка 58/40"},
    {"label_key": "item_5860", "label": "Маркировка 58/60"},
    {"label_key": "item_75120", "label": "Маркировка 75/120"},
    {"label_key": "item_cz", "label": "Маркировка 58/40 (шт/чз)"},
)


def _agent_artifact_path(*, version: str, base_name: str, versioned_name: str):
    artifacts_dir = (settings.BASE_DIR / "static" / "agents").resolve()
    candidates = []
    if version:
        candidates.append((artifacts_dir / versioned_name).resolve())
    candidates.append((artifacts_dir / base_name).resolve())
    for path in candidates:
        if path.exists():
            return path
    return candidates[0]


def _print_agent_targets(agent_name: str) -> list[str]:
    value = str(agent_name or "").strip()
    if not value:
        return []
    targets = [value]
    agent = (
        DeviceAgent.objects.filter(
            models.Q(agent_id=value) | models.Q(name=value) | models.Q(host=value)
        )
        .order_by("-last_seen", "-updated_at")
        .first()
    )
    if agent:
        for candidate in (agent.agent_id, agent.name, agent.host):
            text = str(candidate or "").strip()
            if text and text not in targets:
                targets.append(text)
    return targets


def _print_jobs_for_agent(agent_name: str):
    qs = ProcessingPrintJob.objects.all()
    targets = _print_agent_targets(agent_name)
    if targets:
        qs = qs.filter(agent__in=targets)
    return qs


def _resolve_device_agent(agent_name: str):
    value = str(agent_name or "").strip()
    if not value:
        return None
    return (
        DeviceAgent.objects.filter(
            models.Q(agent_id=value) | models.Q(name=value) | models.Q(host=value)
        )
        .order_by("-last_seen", "-updated_at")
        .first()
    )


def _scanner_health_payload(meta: dict, *, agent_online: bool) -> dict:
    com = meta.get("com") if isinstance(meta.get("com"), dict) else {}
    health = meta.get("com_health") if isinstance(meta.get("com_health"), dict) else {}
    status = meta.get("com_status") if isinstance(meta.get("com_status"), dict) else {}
    configured_port = str(com.get("port") or health.get("port") or "").strip()
    reason = str(health.get("reason") or "").strip()
    ready = health.get("ready") if isinstance(health.get("ready"), bool) else None
    if ready is None and isinstance(status.get("connected"), bool):
        ready = bool(status.get("connected"))
        reason = reason or ("connected" if ready else "not_connected")
    error = str(health.get("error") or status.get("error") or "").strip()

    if not agent_online:
        severity, state, text = "danger", "agent_offline", "Агент не в сети"
    elif com.get("enabled") is False or reason == "disabled":
        severity, state, text = "neutral", "disabled", "Сканер отключен"
    elif ready is True:
        severity, state, text = "ok", "ready", f"Подключен: {configured_port or 'порт не указан'}"
    elif reason == "port_not_found":
        severity, state, text = "danger", reason, f"Порт {configured_port or 'не указан'} не найден"
    elif reason == "port_missing":
        severity, state, text = "danger", reason, "COM-порт не задан"
    elif error:
        severity, state, text = "danger", "error", error
    else:
        severity, state, text = "warning", reason or "not_connected", "Сканер не подключен"
    return {
        "ready": ready,
        "state": state,
        "severity": severity,
        "text": text,
        "port": configured_port,
        "reason": reason,
        "error": error,
    }


def _scanner_port_recommendation(meta: dict, scanner_health: dict) -> dict:
    raw_ports = meta.get("com_ports") or meta.get("ports")
    if isinstance(raw_ports, str):
        raw_ports = [raw_ports]
    if not isinstance(raw_ports, list):
        raw_ports = []

    devices = meta.get("com_devices") if isinstance(meta.get("com_devices"), list) else []
    devices_by_port: dict[str, dict] = {}
    port_names: dict[str, str] = {}

    def remember_port(value) -> str:
        port = str(value or "").strip().upper()
        if not re.fullmatch(r"COM\d+", port, flags=re.IGNORECASE):
            return ""
        port_names.setdefault(port.casefold(), port)
        return port

    for value in raw_ports:
        remember_port(value)
    for raw_device in devices:
        if not isinstance(raw_device, dict):
            continue
        device = dict(raw_device)
        port = remember_port(device.get("port"))
        if not port:
            match = re.search(r"\bCOM\d+\b", str(device.get("name") or ""), flags=re.IGNORECASE)
            port = remember_port(match.group(0) if match else "")
        if port:
            devices_by_port[port.casefold()] = device

    actual_port = str(scanner_health.get("port") or "").strip().upper()
    scanner_ready = scanner_health.get("ready") is True
    candidates = []
    for key, port in port_names.items():
        device = devices_by_port.get(key, {})
        name = str(device.get("name") or device.get("description") or "").strip()
        manufacturer = str(device.get("manufacturer") or "").strip()
        service = str(device.get("service") or "").strip()
        status = str(device.get("status") or "").strip()
        identity = " ".join(
            str(device.get(field) or "")
            for field in ("name", "description", "manufacturer", "service", "pnp_id", "device_id")
        ).casefold()
        is_system = any(
            marker in identity
            for marker in (
                "active management technology",
                "pnp0501",
                "стандартные порты",
                "standard ports",
                "acpi\\",
            )
        )
        is_usb = any(
            marker in identity
            for marker in (
                "usb\\",
                "usb serial",
                "usb-serial",
                "usbser",
                "ch34",
                "ftdi",
                "cp210",
                "prolific",
                "vid_",
                "последовательным интерфейсом usb",
            )
        )
        is_scanner_named = any(
            marker in identity
            for marker in ("scanner", "barcode", "сканер", "сканирования", "honeywell", "datalogic")
        )
        is_connected = scanner_ready and bool(actual_port) and actual_port.casefold() == key
        score = 0
        if is_connected:
            score += 1000
        if is_scanner_named:
            score += 160
        if is_usb:
            score += 100
        if status.casefold() == "ok" or str(device.get("error_code") or "") == "0":
            score += 10
        if is_system:
            score -= 250
        kind = "USB-устройство" if is_usb else "системный порт" if is_system else "COM-устройство"
        candidates.append(
            {
                "port": port,
                "name": name,
                "manufacturer": manufacturer,
                "service": service,
                "status": status,
                "kind": kind,
                "score": score,
                "is_connected": is_connected,
                "is_usb": is_usb,
                "is_system": is_system,
            }
        )

    def port_number(value: str) -> int:
        match = re.search(r"\d+", value)
        return int(match.group(0)) if match else 9999

    candidates.sort(key=lambda item: (-item["score"], port_number(item["port"]), item["port"]))
    recommended = candidates[0] if candidates else None
    recommendation_is_confident = bool(
        recommended
        and (
            recommended["is_connected"]
            or recommended["score"] >= 100
            and (len(candidates) == 1 or recommended["score"] > candidates[1]["score"])
        )
    )
    for item in candidates:
        item["is_recommended"] = bool(recommendation_is_confident and item is recommended)

    if not candidates:
        return {
            "state": "no_ports",
            "severity": "danger",
            "port": "",
            "title": "COM-порт не найден",
            "text": (
                "Windows не видит ни одного COM-порта. Проверьте USB-приемник или кабель, драйвер "
                "и переключите сканер из HID-клавиатуры в USB-COM."
            ),
            "can_select": False,
            "candidates": [],
        }
    if recommendation_is_confident and recommended:
        if recommended["is_connected"]:
            title = f"Выбрать {recommended['port']}"
            text = "Сканер уже подключен к этому порту и передает данные агенту."
            state, severity = "connected", "ok"
        else:
            title = f"Рекомендуется {recommended['port']}"
            device_name = recommended["name"] or recommended["kind"]
            text = f"Windows определяет этот порт как USB-устройство: {device_name}."
            state, severity = "recommended", "warning"
        return {
            "state": state,
            "severity": severity,
            "port": recommended["port"],
            "title": title,
            "text": text,
            "can_select": True,
            "candidates": candidates,
        }
    return {
        "state": "ambiguous",
        "severity": "warning",
        "port": "",
        "title": "Нужно проверить сканированием",
        "text": "Порты найдены, но Windows не позволяет надежно отличить сканер. USB-порты показаны первыми.",
        "can_select": False,
        "candidates": candidates,
    }


def _printer_health_payload(meta: dict, *, agent_online: bool, printer_name: str = "") -> dict:
    label_printers, _ = split_printers_by_kind(agent_printer_names_from_meta(meta))
    requested = str(printer_name or "").strip()
    selected = next((name for name in label_printers if name.casefold() == requested.casefold()), "")
    if not selected and label_printers:
        selected = label_printers[0]
    details = agent_printer_details_from_meta(meta)
    detail = next(
        (
            item
            for item in details
            if str(item.get("name") or "").strip().casefold() == selected.casefold()
        ),
        None,
    ) if selected else None

    if not agent_online:
        severity, state, text = "danger", "agent_offline", "Агент печати не в сети"
    elif not selected:
        severity, state, text = "danger", "not_found", "Принтер этикеток не найден"
    elif detail is None:
        severity, state, text = "warning", "detected_unverified", f"{selected} обнаружен, готовность не подтверждена"
    elif bool(detail.get("is_offline")):
        severity, state, text = "danger", "offline", f"{selected}: нет связи"
    elif bool(detail.get("is_paused")):
        severity, state, text = "warning", "paused", f"{selected}: печать приостановлена"
    elif bool(detail.get("is_busy")):
        severity, state, text = "warning", "busy", f"{selected}: занят"
    else:
        severity, state, text = "ok", "ready", f"{selected}: готов"
    return {
        "state": state,
        "severity": severity,
        "text": text,
        "name": selected,
        "detected": label_printers,
        "details_available": detail is not None,
        "jobs": int((detail or {}).get("jobs") or 0),
    }


def build_equipment_status_payload(*, agent_name: str, printer_name: str = "") -> dict:
    agent = _resolve_device_agent(agent_name)
    if agent is None:
        return {"ok": False, "error": "agent_not_found"}

    now = timezone.now()
    agent_online = bool(agent.last_seen and agent.last_seen >= now - timedelta(seconds=30))
    meta = agent.meta if isinstance(agent.meta, dict) else {}
    scanner = _scanner_health_payload(meta, agent_online=agent_online)
    printer = _printer_health_payload(meta, agent_online=agent_online, printer_name=printer_name)
    scanner_settings = load_scanner_settings()
    desired = scanner_settings.get("default") if isinstance(scanner_settings, dict) else {}
    desired = desired if isinstance(desired, dict) else {}
    desired_port = str(desired.get("port") or "").strip()
    actual_port = str(scanner.get("port") or "").strip()
    config_matches = bool(desired_port and actual_port and desired_port.casefold() == actual_port.casefold())

    latest_command = (
        AgentCommand.objects.filter(
            agent_id=agent.agent_id,
            command__in=("scanner.config", "scanner.reconnect"),
        )
        .order_by("-created_at")
        .first()
    )
    command_payload = latest_command.payload if latest_command and isinstance(latest_command.payload, dict) else {}
    command_port = str(command_payload.get("port") or "").strip()
    if not desired_port:
        apply_severity, apply_state, apply_text = "neutral", "not_configured", "COM-порт в настройках не задан"
    elif config_matches:
        apply_severity, apply_state, apply_text = "ok", "applied", f"Настройки применены: {actual_port}"
    elif latest_command and latest_command.status == AgentCommand.STATUS_FAILED:
        apply_severity, apply_state = "danger", "failed"
        apply_text = latest_command.error or "Агент вернул ошибку применения"
    elif latest_command and latest_command.command == "scanner.config" and not command_port:
        apply_severity, apply_state = "danger", "port_missing_in_command"
        apply_text = "Последняя команда отправлена без COM-порта; настройки не применились"
    elif latest_command and latest_command.status in {AgentCommand.STATUS_PENDING, AgentCommand.STATUS_DELIVERED}:
        apply_severity, apply_state = "warning", latest_command.status
        action = "ожидает агента" if latest_command.status == AgentCommand.STATUS_PENDING else "доставлена, подтверждения нет"
        apply_text = f"Команда {action}: {command_port or desired_port}"
    else:
        apply_severity, apply_state = "warning", "not_applied"
        apply_text = f"Сохранено {desired_port}, на агенте {actual_port or 'порт не указан'}"

    print_targets = _print_agent_targets(agent.agent_id)
    jobs = ProcessingPrintJob.objects.filter(agent__in=print_targets)
    counts = {
        "pending": ProcessingPrintJob.total_copies(jobs.filter(status=ProcessingPrintJob.STATUS_PENDING)),
        "printing": ProcessingPrintJob.total_copies(jobs.filter(status=ProcessingPrintJob.STATUS_PRINTING)),
        "failed": ProcessingPrintJob.total_copies(jobs.filter(status=ProcessingPrintJob.STATUS_FAILED)),
    }
    last_job = jobs.order_by("-updated_at").first()
    paused = bool(load_print_agent_status().get("paused"))
    if paused:
        status_line = "Печать остановлена"
    elif counts["failed"]:
        status_line = f"Есть ошибки печати: {counts['failed']}"
    elif counts["printing"]:
        status_line = f"Печатается: {counts['printing']}"
    elif counts["pending"]:
        status_line = f"Ожидает в очереди: {counts['pending']}"
    else:
        status_line = printer["text"]

    return {
        "ok": True,
        "agent_id": agent.agent_id,
        "agent": {
            "name": str(agent.name or agent.host or agent.agent_id),
            "online": agent_online,
            "severity": "ok" if agent_online else "danger",
            "text": "Fullbox Agent онлайн" if agent_online else "Fullbox Agent не в сети",
            "version": agent.version,
            "last_seen": timezone.localtime(agent.last_seen).strftime("%d.%m.%Y %H:%M:%S") if agent.last_seen else "нет данных",
        },
        "printer": printer,
        "scanner": scanner,
        "application": {
            "state": apply_state,
            "severity": apply_severity,
            "text": apply_text,
            "desired_port": desired_port,
            "actual_port": actual_port,
            "matches": config_matches,
            "last_command_id": latest_command.id if latest_command else None,
            "last_command_status": latest_command.status if latest_command else "",
        },
        "counts": counts,
        "paused": paused,
        "status_line": status_line,
        "last_job_time": timezone.localtime(last_job.updated_at).strftime("%d.%m.%Y %H:%M:%S") if last_job else "",
        "last_error": last_job.error if last_job and last_job.status == ProcessingPrintJob.STATUS_FAILED else "",
    }


def equipment_status_response(*, agent_name: str, printer_name: str = ""):
    payload = build_equipment_status_payload(agent_name=agent_name, printer_name=printer_name)
    return JsonResponse(payload, status=200 if payload.get("ok") else 404)


def build_label_settings_context() -> dict:
    printers, printers_meta = load_available_printers_data()
    label_printers, pdf_printers = split_printers_by_kind(printers)
    label_settings = load_label_settings()
    agent_status = load_print_agent_status()
    agent_name = str(agent_status.get("agent") or "").strip() or "неизвестно"
    last_seen_raw = agent_status.get("last_seen")
    last_seen_text = "нет данных"
    is_online = False
    if last_seen_raw:
        try:
            last_seen = datetime.fromisoformat(str(last_seen_raw))
            if timezone.is_naive(last_seen):
                last_seen = timezone.make_aware(last_seen)
            last_seen_text = timezone.localtime(last_seen).strftime("%d.%m.%Y %H:%M:%S")
            is_online = (timezone.now() - last_seen) <= timedelta(seconds=20)
        except (TypeError, ValueError):
            last_seen_text = str(last_seen_raw)

    agent_jobs_qs = _print_jobs_for_agent(agent_name)
    pending_count = ProcessingPrintJob.total_copies(
        agent_jobs_qs.filter(status=ProcessingPrintJob.STATUS_PENDING)
    )
    printing_count = ProcessingPrintJob.total_copies(
        agent_jobs_qs.filter(status=ProcessingPrintJob.STATUS_PRINTING)
    )
    failed_count = ProcessingPrintJob.total_copies(
        agent_jobs_qs.filter(status=ProcessingPrintJob.STATUS_FAILED)
    )
    last_job = agent_jobs_qs.order_by("-updated_at").first()
    last_error = ""
    last_job_time = ""
    if last_job:
        last_job_time = timezone.localtime(last_job.updated_at).strftime("%d.%m.%Y %H:%M:%S")
        if last_job.status == ProcessingPrintJob.STATUS_FAILED:
            last_error = last_job.error or "ошибка без описания"

    paused = bool(agent_status.get("paused"))
    if paused:
        print_status = "Печать остановлена"
    elif pending_count:
        print_status = f"В очереди: {pending_count}"
        if not is_online:
            print_status = f"{print_status} (агент не активен)"
    elif last_job and last_job.status == ProcessingPrintJob.STATUS_FAILED:
        print_status = "Ошибка печати"
    elif last_job and last_job.status == ProcessingPrintJob.STATUS_PRINTING:
        print_status = "Печать выполняется"
    else:
        print_status = "Готов к печати"

    agent_line = f"{agent_name} · {last_seen_text}" if last_seen_text else agent_name
    label_sample = {
        "article": "КОВРИКИ001",
        "name": "Коврики универсальные",
        "size": "M",
        "brand": "Fullbox",
        "subject": "Коврики",
        "color": "Черный",
        "composition": "Полиэстер",
        "supplier": "Кондель",
        "country": "Россия",
        "barcode_extra": "SKU-0001",
    }
    scanner_settings = load_scanner_settings()
    scanner_default = scanner_settings.get("default") if isinstance(scanner_settings, dict) else {}
    scanner_default = scanner_default if isinstance(scanner_default, dict) else {}
    scanner_updated_raw = scanner_settings.get("updated_at") if isinstance(scanner_settings, dict) else None
    scanner_updated_text = "нет данных"
    if scanner_updated_raw:
        try:
            scanner_updated_at = datetime.fromisoformat(str(scanner_updated_raw))
            if timezone.is_naive(scanner_updated_at):
                scanner_updated_at = timezone.make_aware(scanner_updated_at)
            scanner_updated_text = timezone.localtime(scanner_updated_at).strftime("%d.%m.%Y %H:%M:%S")
        except (TypeError, ValueError):
            scanner_updated_text = str(scanner_updated_raw)
    scanner_updated_by = ""
    if isinstance(scanner_settings, dict):
        scanner_updated_by = str(scanner_settings.get("updated_by") or "").strip()

    agent_items = []
    ports_pool = set()
    agent_qs = DeviceAgent.objects.all().order_by("-last_seen", "-updated_at")
    online_threshold = timezone.now() - timedelta(seconds=30)
    seen_agent_hosts = set()
    for agent in agent_qs:
        agent_title = str(agent.name or agent.host or agent.agent_id or "").strip()
        host_key = str(agent.host or agent.name or "").strip().lower()
        if host_key:
            if host_key in seen_agent_hosts:
                continue
            seen_agent_hosts.add(host_key)
        meta = agent.meta if isinstance(agent.meta, dict) else {}
        com_meta = meta.get("com") if isinstance(meta.get("com"), dict) else {}
        ports = meta.get("com_ports") or meta.get("ports")
        if isinstance(ports, str):
            ports = [ports]
        if not isinstance(ports, list):
            ports = []
        ports = [str(port).strip() for port in ports if str(port).strip()]
        for port in ports:
            ports_pool.add(port)
        agent_label_printers, agent_pdf_printers = split_printers_by_kind(agent_printer_names_from_meta(meta))
        last_seen_text = "нет данных"
        if agent.last_seen:
            try:
                last_seen_text = timezone.localtime(agent.last_seen).strftime("%d.%m.%Y %H:%M:%S")
            except (TypeError, ValueError):
                last_seen_text = str(agent.last_seen)
        is_online = bool(agent.last_seen and agent.last_seen >= online_threshold)
        scanner_health = _scanner_health_payload(meta, agent_online=is_online)
        scanner_port_recommendation = _scanner_port_recommendation(meta, scanner_health)
        printer_health = _printer_health_payload(meta, agent_online=is_online)
        desired_scanner_port = str(scanner_default.get("port") or "").strip()
        actual_scanner_port = str(scanner_health.get("port") or "").strip()
        agent_items.append(
            {
                "agent_id": agent.agent_id,
                "title": agent_title or agent.agent_id,
                "version": agent.version,
                "last_seen": last_seen_text,
                "is_online": is_online,
                "status": "онлайн" if is_online else "нет связи",
                "com": {
                    "enabled": com_meta.get("enabled"),
                    "port": com_meta.get("port"),
                    "baud": com_meta.get("baud"),
                    "eol": com_meta.get("eol"),
                    "idle_ms": com_meta.get("idle_ms") if com_meta.get("idle_ms") is not None else com_meta.get("idle"),
                },
                "ports": ports,
                "printers": agent_label_printers,
                "pdf_printers": agent_pdf_printers,
                "scanner_health": scanner_health,
                "scanner_port_recommendation": scanner_port_recommendation,
                "printer_health": printer_health,
                "scanner_desired_port": desired_scanner_port,
                "scanner_config_matches": bool(
                    desired_scanner_port
                    and actual_scanner_port
                    and desired_scanner_port.casefold() == actual_scanner_port.casefold()
                ),
            }
        )
    processing_template_bindings = load_processing_param_template_bindings()
    processing_param_templates = []
    for item in PROCESSING_PARAM_TEMPLATE_FIELDS:
        label_key = str(item.get("label_key") or "").strip()
        processing_param_templates.append(
            {
                "label_key": label_key,
                "label": item.get("label") or label_key,
                "selected_template_key": resolve_processing_param_template_key(
                    label_key,
                    processing_template_bindings,
                ),
                "options": [
                    get_effective_label_template(option.get("key"), label_settings)
                    for option in processing_param_template_options(label_key)
                ],
            }
        )
    label_templates = [
        get_effective_label_template(item.get("key"), label_settings)
        for item in LABEL_TEMPLATES
    ]
    label_template_sections = [
        {
            "title": "Товарные этикетки",
            "templates": [item for item in label_templates if item.get("category") == "product"],
        },
        {
            "title": "Складские этикетки",
            "templates": [item for item in label_templates if item.get("category") == "storage"],
        },
        {
            "title": "Документы отгрузки",
            "templates": [item for item in label_templates if item.get("category") == "shipping"],
        },
    ]
    return {
        "label_sizes": LABEL_SIZES,
        "label_templates": label_templates,
        "label_template_sections": label_template_sections,
        "available_printers": label_printers,
        "available_label_printers": label_printers,
        "available_pdf_printers": pdf_printers,
        "available_printers_all": printers,
        "available_printers_meta": printers_meta,
        "label_sample": label_sample,
        "label_sample_barcode": "4601234567890",
        "label_settings": label_settings,
        "processing_param_template_bindings": processing_template_bindings,
        "processing_param_templates": processing_param_templates,
        "print_agents": agent_items,
        "print_status_line": print_status,
        "print_agent_line": agent_line,
        "print_last_error": last_error,
        "print_last_job_time": last_job_time,
        "print_queue_pending": pending_count,
        "print_queue_printing": printing_count,
        "print_queue_failed": failed_count,
        "print_paused": paused,
        "scanner_settings": scanner_settings,
        "scanner_default": scanner_default,
        "scanner_updated_at": scanner_updated_text,
        "scanner_updated_by": scanner_updated_by,
        "scanner_agents": agent_items,
        "scanner_ports": sorted(ports_pool),
    }


def save_label_settings_request(*, body: bytes):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    key = (payload.get("key") or "").strip()
    processing_bindings_payload = (
        payload.get("processing_param_templates")
        if isinstance(payload.get("processing_param_templates"), dict)
        else None
    )
    if key and key not in LABEL_TEMPLATE_KEYS:
        return JsonResponse({"ok": False, "error": "invalid_key"}, status=400)
    if not key and processing_bindings_payload is None:
        return JsonResponse({"ok": False, "error": "invalid_key"}, status=400)
    text_data = payload.get("text") if isinstance(payload.get("text"), dict) else {}
    font_data = payload.get("fonts") if isinstance(payload.get("fonts"), dict) else {}
    enabled_data = payload.get("enabled") if isinstance(payload.get("enabled"), dict) else None
    dimensions_data = payload.get("dimensions")
    cleaned_dimensions = clean_label_dimensions(dimensions_data)
    if dimensions_data is not None and (
        not isinstance(dimensions_data, dict)
        or set(cleaned_dimensions) != {"width_mm", "height_mm"}
    ):
        return JsonResponse({"ok": False, "error": "invalid_dimensions"}, status=400)
    cleaned_text = {}
    for field in LABEL_FIELDS:
        if field in text_data:
            cleaned_text[field] = str(text_data.get(field) or "").strip()
    cleaned_fonts = {}
    for field in LABEL_FIELDS:
        if field not in font_data:
            continue
        try:
            value = float(str(font_data.get(field)).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        cleaned_fonts[field] = value
    cleaned_enabled = clean_label_enabled(enabled_data) if enabled_data is not None else None
    if key:
        settings_payload = load_label_settings()
        entry = {"text": cleaned_text, "fonts": cleaned_fonts}
        if cleaned_enabled is not None:
            entry["enabled"] = cleaned_enabled
        elif isinstance(settings_payload.get(key), dict) and settings_payload.get(key, {}).get("enabled"):
            entry["enabled"] = settings_payload[key]["enabled"]
        if dimensions_data is not None:
            entry["dimensions"] = cleaned_dimensions
        elif isinstance(settings_payload.get(key), dict) and settings_payload.get(key, {}).get("dimensions"):
            entry["dimensions"] = settings_payload[key]["dimensions"]
        settings_payload[key] = entry
        settings_payload = synchronize_product_label_settings(
            settings_payload,
            source_key=key,
        )
        save_label_settings(settings_payload)
    if processing_bindings_payload is not None:
        save_processing_param_template_bindings(processing_bindings_payload)
    return JsonResponse(
        {
            "ok": True,
            "key": key,
            "dimensions": cleaned_dimensions if dimensions_data is not None else None,
            "processing_param_templates": load_processing_param_template_bindings(),
        }
    )


def parse_json_body(body: bytes):
    try:
        body_text = body.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None
    if not body_text:
        return {}
    try:
        return json.loads(body_text)
    except json.JSONDecodeError:
        return None


def scanner_settings_save_response(*, body: bytes, user=None):
    payload = parse_json_body(body)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    settings_payload = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_payload, dict):
        settings_payload = payload if isinstance(payload, dict) else {}
    updated_by = ""
    if user and getattr(user, "is_authenticated", False):
        updated_by = user.get_full_name() or user.username
    normalized = save_scanner_settings(settings_payload, updated_by=updated_by or None, when=timezone.now())
    return JsonResponse({"ok": True, "settings": normalized})


def _scanner_port_seen_recently(*, agent_id: str, port: str) -> bool:
    requested_port = str(port or "").strip().upper()
    if not re.fullmatch(r"COM\d+", requested_port, flags=re.IGNORECASE):
        return False
    cutoff = timezone.now() - timedelta(minutes=15)
    payloads = (
        AgentEvent.objects.filter(
            agent_id=agent_id,
            event_type=AgentEvent.EVENT_SCAN,
            created_at__gte=cutoff,
        )
        .order_by("-id")
        .values_list("payload", flat=True)[:100]
    )
    return any(
        isinstance(payload, dict)
        and str(payload.get("port") or "").strip().upper() == requested_port
        for payload in payloads
    )


def scanner_settings_apply_response(*, body: bytes):
    payload = parse_json_body(body)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    settings_payload = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_payload, dict):
        settings_payload = payload if isinstance(payload, dict) else {}
    normalized = normalize_scanner_settings(settings_payload)
    config = normalized.get("default") if isinstance(normalized, dict) else None
    if not isinstance(config, dict):
        return JsonResponse({"ok": False, "error": "invalid_settings"}, status=400)
    target_agent = str(payload.get("agent_id") or "").strip() if isinstance(payload, dict) else ""
    scope = str(payload.get("scope") or "").strip().lower() if isinstance(payload, dict) else ""
    reconnect_only = bool(payload.get("reconnect_only")) if isinstance(payload, dict) else False
    reconnect = bool(payload.get("reconnect")) if isinstance(payload, dict) else False

    if target_agent:
        agents = list(DeviceAgent.objects.filter(agent_id=target_agent))
    else:
        qs = DeviceAgent.objects.all()
        if scope != "all":
            online_since = timezone.now() - timedelta(seconds=60)
            qs = qs.filter(last_seen__gte=online_since)
        agents = list(qs)
    if not agents:
        return JsonResponse({"ok": False, "error": "no_agents"}, status=404)

    command_ids = []
    applied_agent_ids = []
    skipped_agents = []
    for agent in agents:
        agent_id = agent.agent_id
        if not reconnect_only:
            requested_port = str(config.get("port") or "").strip()
            meta = agent.meta if isinstance(agent.meta, dict) else {}
            raw_ports = meta.get("com_ports") or meta.get("ports")
            if isinstance(raw_ports, str):
                raw_ports = [raw_ports]
            if not isinstance(raw_ports, list):
                raw_ports = []
            known_ports = [
                normalize_scanner_settings({"port": value})["default"]["port"]
                for value in raw_ports
                if str(value or "").strip()
            ]
            health = meta.get("com_health") if isinstance(meta.get("com_health"), dict) else {}
            explicitly_missing = health.get("port_present") is False
            unavailable = bool(known_ports and requested_port not in known_ports)
            unavailable = unavailable or bool(not known_ports and explicitly_missing)
            if unavailable and _scanner_port_seen_recently(agent_id=agent_id, port=requested_port):
                unavailable = False
            if unavailable:
                skipped_agents.append(
                    {
                        "agent_id": agent_id,
                        "requested_port": requested_port,
                        "available_ports": known_ports,
                        "reason": "port_not_available",
                    }
                )
                continue
        if reconnect_only:
            command = "scanner.reconnect"
            cmd_payload = {"source": "labels"}
        else:
            command = "scanner.config"
            cmd_payload = {
                "enabled": config.get("enabled"),
                "port": config.get("port"),
                "baud": config.get("baud"),
                "eol": config.get("eol"),
                "idle_ms": config.get("idle_ms"),
                "reconnect": reconnect,
                "source": "labels",
            }
        created = AgentCommand.objects.create(agent_id=agent_id, command=command, payload=cmd_payload)
        command_ids.append(created.id)
        applied_agent_ids.append(agent_id)
    if not command_ids:
        return JsonResponse(
            {
                "ok": False,
                "error": "port_not_available",
                "requested_port": str(config.get("port") or "").strip(),
                "skipped_agents": skipped_agents,
            },
            status=409,
        )
    return JsonResponse(
        {
            "ok": True,
            "count": len(command_ids),
            "agent_ids": applied_agent_ids,
            "command_ids": command_ids,
            "skipped_agents": skipped_agents,
        }
    )


def scanner_test_response(*, body: bytes):
    payload = parse_json_body(body)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    settings_payload = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(settings_payload, dict):
        settings_payload = payload if isinstance(payload, dict) else {}
    normalized = normalize_scanner_settings(settings_payload)
    config = normalized.get("default") if isinstance(normalized, dict) else None
    if not isinstance(config, dict):
        return JsonResponse({"ok": False, "error": "invalid_settings"}, status=400)
    target_agent = str(payload.get("agent_id") or "").strip() if isinstance(payload, dict) else ""
    if not target_agent:
        return JsonResponse({"ok": False, "error": "agent_required"}, status=400)
    cmd_payload = {
        "port": config.get("port"),
        "baud": config.get("baud"),
        "eol": config.get("eol"),
        "idle_ms": config.get("idle_ms"),
        "source": "labels",
    }
    command = AgentCommand.objects.create(agent_id=target_agent, command="scanner.test", payload=cmd_payload)
    return JsonResponse({"ok": True, "command_id": command.id})


def scanner_test_status_response(*, command_id: int):
    try:
        command = AgentCommand.objects.get(pk=command_id)
    except AgentCommand.DoesNotExist:
        return JsonResponse({"ok": False, "error": "not_found"}, status=404)
    return JsonResponse(
        {
            "ok": True,
            "status": command.status,
            "command": command.command,
            "agent_id": command.agent_id,
            "result": command.result,
            "error": command.error,
            "acked_at": command.acked_at.isoformat() if command.acked_at else "",
        }
    )


def scanner_detect_start_response(*, body: bytes):
    payload = parse_json_body(body)
    if payload is None:
        return JsonResponse({"ok": False, "error": "invalid_json"}, status=400)
    target_agent = str(payload.get("agent_id") or "").strip() if isinstance(payload, dict) else ""
    if not target_agent:
        return JsonResponse({"ok": False, "error": "agent_required"}, status=400)
    agent = DeviceAgent.objects.filter(agent_id=target_agent).first()
    if agent is None:
        return JsonResponse({"ok": False, "error": "agent_not_found"}, status=404)

    baseline_event_id = (
        AgentEvent.objects.filter(agent_id=target_agent, event_type=AgentEvent.EVENT_SCAN)
        .order_by("-id")
        .values_list("id", flat=True)
        .first()
        or 0
    )
    agent_online = bool(agent.last_seen and agent.last_seen >= timezone.now() - timedelta(seconds=30))
    return JsonResponse(
        {
            "ok": True,
            "agent_id": target_agent,
            "agent_online": agent_online,
            "baseline_event_id": baseline_event_id,
        }
    )


def scanner_detect_status_response(*, agent_id: str, after_id):
    target_agent = str(agent_id or "").strip()
    if not target_agent:
        return JsonResponse({"ok": False, "error": "agent_required"}, status=400)
    try:
        baseline_event_id = max(0, int(after_id or 0))
    except (TypeError, ValueError):
        return JsonResponse({"ok": False, "error": "invalid_after_id"}, status=400)
    if not DeviceAgent.objects.filter(agent_id=target_agent).exists():
        return JsonResponse({"ok": False, "error": "agent_not_found"}, status=404)

    event = (
        AgentEvent.objects.filter(
            agent_id=target_agent,
            event_type=AgentEvent.EVENT_SCAN,
            id__gt=baseline_event_id,
        )
        .order_by("id")
        .first()
    )
    if event is None:
        return JsonResponse(
            {
                "ok": True,
                "agent_id": target_agent,
                "detected": False,
                "after_event_id": baseline_event_id,
            }
        )

    event_payload = event.payload if isinstance(event.payload, dict) else {}
    raw_port = str(event_payload.get("port") or "").strip().upper()
    port = raw_port if re.fullmatch(r"COM\d+", raw_port, flags=re.IGNORECASE) else ""
    source = str(event_payload.get("source") or "").strip().lower()
    return JsonResponse(
        {
            "ok": True,
            "agent_id": target_agent,
            "detected": bool(port),
            "event_seen": True,
            "after_event_id": event.id,
            "mode": "com" if port else source or "unknown",
            "port": port,
            "scanned_at": event.created_at.isoformat(),
        }
    )


def download_fullbox_agent_bundle_response(*, bundle_format: str | None):
    version = AGENT_VERSION.strip()
    zip_name = "fullbox_agent_bundle.zip"
    exe_name = "Fullbox.Agent.Setup.exe"
    if version:
        zip_name = f"fullbox_agent_bundle_v{version}.zip"
        exe_name = f"Fullbox.Agent.Setup.v{version}.exe"
    zip_path = _agent_artifact_path(
        version=version,
        base_name="fullbox_agent_bundle.zip",
        versioned_name=zip_name,
    )
    if bundle_format == "zip":
        if not zip_path.exists():
            return HttpResponseBadRequest("bundle_not_found")
        response = FileResponse(zip_path.open("rb"), content_type="application/zip")
        response["Content-Disposition"] = f"attachment; filename={zip_name}"
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
    setup_path = _agent_artifact_path(
        version=version,
        base_name="Fullbox.Agent.Setup.exe",
        versioned_name=exe_name,
    )
    if setup_path.exists():
        response = FileResponse(setup_path.open("rb"), content_type="application/octet-stream")
        response["Content-Disposition"] = f"attachment; filename={exe_name}"
        response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
    if not zip_path.exists():
        return HttpResponseBadRequest("bundle_not_found")
    response = FileResponse(zip_path.open("rb"), content_type="application/zip")
    response["Content-Disposition"] = f"attachment; filename={zip_name}"
    return response
