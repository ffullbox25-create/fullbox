from __future__ import annotations

import base64
import re
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from employees.models import Employee
from fbs.exceptions import FbsFeatureDisabled, FbsLabelError
from fbs.flags import feature_enabled
from fbs.models import (
    FbsHandoverBatch,
    FbsHandoverBox,
    FbsHandoverOrder,
    FbsIntegrationProfile,
    FbsOrderItem,
    FbsOrderLabel,
    FbsPickBatch,
    FbsWorkstation,
)
from processing_app.models import ProcessingPrintJob


PRINT_ROLES = {"fbs_controller", "storekeeper", "head_manager", "director", "admin"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PDF_SIGNATURE = b"%PDF-"
MAX_LABEL_BYTES = 20 * 1024 * 1024
FBS_LABEL_WIDTH_MM = 58
FBS_LABEL_HEIGHT_MM = 40
FBS_AUTO_PRINT_RETRY_MARKER = "Автоматический повтор FBS после тайм-аута Desktop."


def fbs_desktop_print_lease_seconds() -> int:
    """Return the shared acknowledgement lease for Fullbox Desktop jobs."""

    return max(
        60,
        int(getattr(settings, "FBS_DESKTOP_PRINT_LEASE_SECONDS", 60) or 60),
    )


@transaction.atomic
def recover_stale_fbs_order_label_print_job(
    *,
    label_id: int,
    not_before=None,
) -> ProcessingPrintJob | None:
    """Return one stale FBS label job to the same Desktop queue.

    The same retained job and payload are reused, so recovery cannot create a
    second queue row for the order label.  One automatic retry is allowed;
    after that the controller keeps the explicit manual retry path instead of
    producing labels indefinitely when a printer is offline.
    """

    stale_cutoff = timezone.now() - timedelta(
        seconds=fbs_desktop_print_lease_seconds()
    )
    label_id = int(label_id)
    jobs = ProcessingPrintJob.objects.select_for_update().filter(
        Q(card_id__startswith=f"fbs:order-label:{label_id}:")
        | Q(card_id__startswith=f"fbs:ozon-order-qr:{label_id}:"),
        status=ProcessingPrintJob.STATUS_PRINTING,
        updated_at__lt=stale_cutoff,
    )
    if not_before is not None:
        jobs = jobs.filter(created_at__gte=not_before)
    job = jobs.order_by("-created_at", "-id").first()
    if job is None or str(job.error or "").startswith(FBS_AUTO_PRINT_RETRY_MARKER):
        return None
    job.status = ProcessingPrintJob.STATUS_PENDING
    job.error = FBS_AUTO_PRINT_RETRY_MARKER
    job.save(update_fields=["status", "error", "updated_at"])
    return job


def _require_printing_enabled() -> None:
    if not feature_enabled("module"):
        raise FbsFeatureDisabled("Модуль FBS выключен.")
    if not feature_enabled("warehouse_writes"):
        raise FbsFeatureDisabled("Операции FBS временно отключены.")


def _print_actor(user, *, required: bool):
    actor = user if getattr(user, "is_authenticated", False) else None
    if actor is None:
        if required:
            raise FbsLabelError("Не указан сотрудник, отправляющий этикетку на печать.")
        return None
    allowed = actor.is_superuser or actor.username == "dev" or Employee.objects.filter(
        user=actor,
        is_active=True,
        role__in=PRINT_ROLES,
    ).exists()
    if not allowed:
        raise FbsLabelError("У сотрудника нет доступа к печати FBS-этикеток.")
    return actor


def _configured_workstation(
    workstation_id: int,
    *,
    desktop_agent_id: str = "",
    desktop_printer_name: str = "",
) -> FbsWorkstation:
    workstation = (
        FbsWorkstation.objects.select_related("device_agent")
        .filter(pk=workstation_id, is_active=True)
        .first()
    )
    if workstation is None:
        raise FbsLabelError("Рабочее место FBS не найдено или отключено.")
    desktop_agent_id = str(desktop_agent_id or "").strip()
    desktop_printer_name = str(desktop_printer_name or "").strip()
    if desktop_agent_id and not desktop_printer_name:
        raise FbsLabelError("В Fullbox Desktop не выбран принтер для этого задания.")
    if not desktop_agent_id and not str(workstation.printer_name or "").strip():
        raise FbsLabelError("Для рабочего места не настроен принтер этикеток.")
    if not desktop_agent_id and workstation.device_agent_id is None:
        raise FbsLabelError("Для рабочего места не привязан агент печати.")
    return workstation


def configured_fbs_print_workstations():
    return (
        FbsWorkstation.objects.select_related("device_agent")
        .filter(
            is_active=True,
            device_agent__isnull=False,
        )
        .exclude(printer_name="")
        .order_by("name", "id")
    )


def _order_workstation(order_id: int, *, desktop: bool = False) -> FbsWorkstation | None:
    batches = FbsPickBatch.objects.select_related("workstation__device_agent").filter(
        tasks__order_id=order_id,
        workstation__is_active=True,
    )
    if not desktop:
        batches = batches.filter(workstation__device_agent__isnull=False).exclude(
            workstation__printer_name=""
        )
    batch = batches.order_by("-created_at", "-id").first()
    return batch.workstation if batch is not None else None


def _read_file(file_field) -> bytes:
    if not file_field:
        raise FbsLabelError("Файл этикетки еще не получен.")
    try:
        file_field.open("rb")
        try:
            content = file_field.read(MAX_LABEL_BYTES + 1)
        finally:
            file_field.close()
    except OSError as exc:
        raise FbsLabelError(
            "Не удалось прочитать файл этикетки. Обновите страницу или обратитесь к администратору."
        ) from exc
    if not content or len(content) > MAX_LABEL_BYTES:
        raise FbsLabelError("Размер файла этикетки недопустим.")
    return content


def _read_png(file_field) -> bytes:
    content = _read_file(file_field)
    if not content.startswith(PNG_SIGNATURE):
        raise FbsLabelError("Файл этикетки не является корректным PNG.")
    return content


def _read_order_label_image(label: FbsOrderLabel) -> tuple[bytes, int, int]:
    content = _read_file(label.file)
    if label.label_format == FbsOrderLabel.FORMAT_PNG:
        if not content.startswith(PNG_SIGNATURE):
            raise FbsLabelError("Файл этикетки не является корректным PNG.")
        return content, FBS_LABEL_WIDTH_MM, FBS_LABEL_HEIGHT_MM
    if label.label_format != FbsOrderLabel.FORMAT_PDF or not content.startswith(PDF_SIGNATURE):
        raise FbsLabelError("Автоматическая печать доступна для PNG и PDF-этикеток.")
    try:
        import fitz

        document = fitz.open(stream=content, filetype="pdf")
        try:
            if document.page_count != 1:
                raise FbsLabelError("PDF заказа должен содержать ровно одну этикетку.")
            page = document.load_page(0)
            width_mm = max(int(round(float(page.rect.width) * 25.4 / 72)), 1)
            height_mm = max(int(round(float(page.rect.height) * 25.4 / 72)), 1)
            pixmap = page.get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72), alpha=False)
            png = pixmap.tobytes("png")
        finally:
            document.close()
    except FbsLabelError:
        raise
    except Exception as exc:
        raise FbsLabelError("Не удалось преобразовать PDF-этикетку для агента печати.") from exc
    if not png.startswith(PNG_SIGNATURE) or len(png) > MAX_LABEL_BYTES:
        raise FbsLabelError("Преобразованная PDF-этикетка имеет недопустимый размер.")
    return png, width_mm, height_mm


def _queue_png(
    *,
    workstation: FbsWorkstation,
    card_id: str,
    order_id: str,
    article: str,
    barcode: str,
    content: bytes,
    actor,
    deduplicate: bool,
    label_width_mm: int = 58,
    label_height_mm: int = 40,
    desktop_agent_id: str = "",
    desktop_printer_name: str = "",
) -> ProcessingPrintJob:
    target_agent = str(desktop_agent_id or "").strip()
    target_printer = str(desktop_printer_name or "").strip()
    if not target_agent:
        target_agent = str(workstation.device_agent.agent_id or "").strip()
    if not target_printer:
        target_printer = str(workstation.printer_name or "").strip()
    if deduplicate:
        existing = ProcessingPrintJob.objects.filter(card_id=card_id).order_by("id").first()
        if existing is not None:
            return existing
    if not deduplicate:
        stale_cutoff = timezone.now() - timedelta(
            seconds=fbs_desktop_print_lease_seconds()
        )
        open_jobs = list(
            ProcessingPrintJob.objects.select_for_update()
            .filter(
                Q(card_id=card_id) | Q(card_id__startswith=f"{card_id}:reprint:"),
                agent=target_agent,
                printer_name=target_printer,
                status__in=(
                    ProcessingPrintJob.STATUS_PENDING,
                    ProcessingPrintJob.STATUS_PRINTING,
                ),
            )
            .order_by("-id")
        )
        for open_job in open_jobs:
            if (
                open_job.status == ProcessingPrintJob.STATUS_PENDING
                or open_job.updated_at >= stale_cutoff
            ):
                return open_job
        # A Desktop crash can leave the latest job in ``printing``.  Reuse the
        # same retained payload after its lease expires instead of creating a
        # second physical label.  The direct-poll path uses the same setting
        # and coordinates through a row lock, so a retry and a poll cannot
        # claim two copies of one order label.
        stale_job = next(
            (
                open_job
                for open_job in open_jobs
                if open_job.status == ProcessingPrintJob.STATUS_PRINTING
            ),
            None,
        )
        if stale_job is not None:
            stale_job.status = ProcessingPrintJob.STATUS_PENDING
            stale_job.error = ""
            stale_job.save(update_fields=["status", "error", "updated_at"])
            return stale_job
        card_id = f"{card_id}:reprint:{uuid4().hex[:12]}"
    return ProcessingPrintJob.objects.create(
        order_id=order_id,
        card_id=card_id[:128],
        article=article[:128],
        barcode=str(barcode or "")[:128],
        printer_name=target_printer,
        label_png_base64=base64.b64encode(content).decode("ascii"),
        copies_count=1,
        label_width_mm=label_width_mm,
        label_height_mm=label_height_mm,
        requested_by=getattr(actor, "username", "") if actor else "marketplace",
        agent=target_agent,
    )


@transaction.atomic
def queue_fbs_order_label_print(
    *,
    label_id: int,
    requested_by=None,
    force: bool = False,
    print_scope: str = "",
    desktop_agent_id: str = "",
    desktop_printer_name: str = "",
) -> ProcessingPrintJob | None:
    _require_printing_enabled()
    actor = _print_actor(requested_by, required=force)
    label = (
        FbsOrderLabel.objects.select_for_update()
        .select_related("order")
        .get(pk=label_id)
    )
    from .pick_restock import order_is_client_canceled_by_marketplace

    if order_is_client_canceled_by_marketplace(label.order):
        if force:
            raise FbsLabelError(
                "Заказ отменен маркетплейсом. Его этикетку печатать нельзя; "
                "переместите товар в тару отмененных заказов."
            )
        return None
    payload = label.payload if isinstance(label.payload, dict) else {}
    uses_preconfirmed_ozon_qr = bool(
        label.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        and payload.get("_preloaded_ozon_order_barcode") is True
        and payload.get("_preconfirmed_order_barcode_at")
    )
    if uses_preconfirmed_ozon_qr:
        if force:
            raise FbsLabelError(
                "Заказ Ozon уже подтвержден по рабочему QR. Официальный PDF "
                "сохранен для архива и повторно не печатается. Если рабочая "
                "этикетка повреждена, повторите печать рабочего QR."
            )
        return None
    if label.status not in {FbsOrderLabel.STATUS_READY, FbsOrderLabel.STATUS_APPLIED}:
        if force:
            raise FbsLabelError("Этикетка marketplace еще не готова к печати.")
        return None
    if label.label_format not in {
        FbsOrderLabel.FORMAT_PNG,
        FbsOrderLabel.FORMAT_PDF,
    } or not label.file:
        if force:
            raise FbsLabelError("Автоматическая печать доступна для PNG и PDF-этикеток.")
        return None
    desktop_agent_id = str(desktop_agent_id or "").strip()
    desktop_printer_name = str(desktop_printer_name or "").strip()
    workstation = _order_workstation(label.order_id, desktop=bool(desktop_agent_id))
    if workstation is None:
        if force:
            raise FbsLabelError(
                "У волны нет рабочего места."
                if desktop_agent_id
                else "У волны нет рабочего места с принтером и агентом печати."
            )
        return None
    if desktop_agent_id and not desktop_printer_name:
        raise FbsLabelError("В Fullbox Desktop не выбран принтер для этого задания.")
    content, _, _ = _read_order_label_image(label)
    key = f"fbs:order-label:{label.id}:{str(label.content_hash or '')[:16]}"
    print_scope = str(print_scope or "").strip()
    if print_scope:
        scope_hash = sha256(print_scope.encode("utf-8")).hexdigest()[:16]
        key = f"{key}:scope:{scope_hash}"
    return _queue_png(
        workstation=workstation,
        card_id=key,
        order_id=f"FBS-ORDER-{label.order_id}",
        article=label.order.external_order_id,
        barcode=label.barcode,
        content=content,
        actor=actor,
        deduplicate=bool(print_scope) or not force,
        label_width_mm=FBS_LABEL_WIDTH_MM,
        label_height_mm=FBS_LABEL_HEIGHT_MM,
        desktop_agent_id=desktop_agent_id,
        desktop_printer_name=desktop_printer_name,
    )


def _render_preloaded_ozon_order_label(*, barcode: str, external_order_id: str) -> bytes:
    """Render the one order QR from the stable barcode received with the posting."""
    try:
        from io import BytesIO

        import qrcode
        from PIL import Image, ImageDraw, ImageFont

        width, height = 685, 472
        canvas = Image.new("RGB", (width, height), "white")
        qr = qrcode.QRCode(version=None, box_size=10, border=2)
        qr.add_data(str(barcode))
        qr.make(fit=True)
        qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        resampling = getattr(getattr(Image, "Resampling", Image), "NEAREST")
        qr_image = qr_image.resize((350, 350), resampling)
        canvas.paste(qr_image, (20, 20))

        def font(size: int, *, bold: bool = False):
            path = (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            )
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                return ImageFont.load_default()

        draw = ImageDraw.Draw(canvas)
        x = 390
        draw.text((x, 24), "OZON", fill="black", font=font(42, bold=True))
        draw.text((x, 82), "ЗАКАЗ", fill="black", font=font(27, bold=True))
        draw.multiline_text(
            (x, 128),
            str(external_order_id).replace("-", "-\n", 1),
            fill="black",
            font=font(27, bold=True),
            spacing=4,
        )
        draw.text((28, 382), str(barcode), fill="black", font=font(30, bold=True))
        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()
    except Exception as exc:
        raise FbsLabelError("Не удалось сформировать QR заказа Ozon.") from exc


def ozon_handover_label_summary(batch: FbsHandoverBatch) -> dict:
    """Return the exact physical composition printed on an internal Ozon label."""
    if batch.profile.marketplace != FbsIntegrationProfile.MARKETPLACE_OZON:
        raise FbsLabelError("Внутренняя этикетка доступна только для Ozon.")
    order_ids = set(
        FbsHandoverOrder.objects.filter(
            box__batch=batch,
            status=FbsHandoverOrder.STATUS_ACTIVE,
        ).values_list("order_id", flat=True)
    )
    if not order_ids:
        raise FbsLabelError("В транспортных коробах Ozon нет товара для этикетки.")
    box_count = (
        FbsHandoverBox.objects.filter(
            batch=batch,
            orders__status=FbsHandoverOrder.STATUS_ACTIVE,
        )
        .distinct()
        .count()
    )
    unit_count = int(
        FbsOrderItem.objects.filter(order_id__in=order_ids).aggregate(total=Sum("quantity"))[
            "total"
        ]
        or 0
    )
    if unit_count <= 0 or box_count <= 0:
        raise FbsLabelError("Состав коробов Ozon пуст или повреждён.")
    agency = batch.profile.agency
    agency_name = str(
        getattr(agency, "short_name", "")
        or getattr(agency, "agn_name", "")
        or agency
    ).strip()
    agency_name = re.sub(
        r"общество\s+с\s+ограниченной\s+ответственностью",
        "ООО",
        agency_name,
        flags=re.IGNORECASE,
    )
    agency_name = re.sub(
        r"индивидуальный\s+предприниматель",
        "ИП",
        agency_name,
        flags=re.IGNORECASE,
    )
    return {
        "batch_id": batch.id,
        "order_count": len(order_ids),
        "unit_count": unit_count,
        "box_count": box_count,
        "agency_name": agency_name,
        "qr_value": f"FBS-HANDOVER-{batch.id}",
    }


def render_ozon_handover_internal_label(batch: FbsHandoverBatch) -> bytes:
    """Render a 58x40 internal reconciliation label, not an Ozon shipping barcode."""
    summary = ozon_handover_label_summary(batch)
    try:
        from io import BytesIO

        import qrcode
        from PIL import Image, ImageDraw, ImageFont

        width, height = 685, 472
        canvas = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(canvas)

        def font(size: int, *, bold: bool = False):
            path = (
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                if bold
                else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
            )
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                return ImageFont.load_default()

        def fit_text(value: str, *, max_width: int, size: int, bold: bool = False):
            value = str(value or "").strip()
            selected = font(size, bold=bold)
            if draw.textbbox((0, 0), value, font=selected)[2] <= max_width:
                return value, selected
            while len(value) > 2:
                value = value[:-1]
                candidate = value.rstrip() + "…"
                if draw.textbbox((0, 0), candidate, font=selected)[2] <= max_width:
                    return candidate, selected
            return "…", selected

        qr = qrcode.QRCode(version=None, box_size=10, border=2)
        qr.add_data(summary["qr_value"])
        qr.make(fit=True)
        qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        resampling = getattr(getattr(Image, "Resampling", Image), "NEAREST")
        qr_image = qr_image.resize((270, 270), resampling)
        canvas.paste(qr_image, (18, 48))

        draw.text((18, 12), "ВНУТРЕННИЙ УЧЁТ FULLBOX", fill="black", font=font(24, bold=True))
        x = 305
        draw.text((x, 42), "OZON FBS", fill="black", font=font(36, bold=True))
        draw.text(
            (x, 92),
            f"ОТГРУЗКА №{summary['batch_id']}",
            fill="black",
            font=font(36, bold=True),
        )
        draw.text(
            (x, 150),
            f"{summary['unit_count']} ШТ.",
            fill="black",
            font=font(68, bold=True),
        )
        draw.text(
            (x, 244),
            f"Заказов: {summary['order_count']}",
            fill="black",
            font=font(28, bold=True),
        )
        draw.text(
            (x, 285),
            f"Коробов: {summary['box_count']}",
            fill="black",
            font=font(28, bold=True),
        )
        draw.line((18, 338, width - 18, 338), fill="black", width=3)
        agency_text, agency_font = fit_text(
            summary["agency_name"], max_width=width - 36, size=31, bold=True
        )
        draw.text((18, 352), agency_text, fill="black", font=agency_font)
        draw.text(
            (18, 405),
            f"QR: {summary['qr_value']} · не является ШК Ozon",
            fill="black",
            font=font(20),
        )
        output = BytesIO()
        canvas.save(output, format="PNG", optimize=True)
        return output.getvalue()
    except FbsLabelError:
        raise
    except Exception as exc:
        raise FbsLabelError("Не удалось сформировать этикетку отгрузки Ozon.") from exc


@transaction.atomic
def queue_fbs_preloaded_ozon_order_label_print(
    *,
    label_id: int,
    requested_by=None,
    force: bool = False,
    desktop_agent_id: str = "",
    desktop_printer_name: str = "",
) -> ProcessingPrintJob | None:
    """Print the order QR immediately from the posting data already in Fullbox."""
    _require_printing_enabled()
    actor = _print_actor(requested_by, required=force)
    label = (
        FbsOrderLabel.objects.select_for_update()
        .select_related("order")
        .get(pk=label_id)
    )
    from .pick_restock import order_is_client_canceled_by_marketplace

    if order_is_client_canceled_by_marketplace(label.order):
        if force:
            raise FbsLabelError(
                "Заказ отменен маркетплейсом. Его QR печатать нельзя; "
                "переместите товар в тару отмененных заказов."
            )
        return None
    payload = label.payload if isinstance(label.payload, dict) else {}
    is_preloaded = bool(
        label.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
        and str(label.barcode or "").strip()
        and payload.get("_preloaded_ozon_order_barcode") is True
        and (
            label.status == FbsOrderLabel.STATUS_REQUESTED
            or (
                label.status == FbsOrderLabel.STATUS_APPLIED
                and payload.get("_preconfirmed_order_barcode_at")
            )
        )
    )
    if not is_preloaded:
        if force:
            raise FbsLabelError("QR заказа Ozon не подготовлен.")
        return None
    desktop_agent_id = str(desktop_agent_id or "").strip()
    desktop_printer_name = str(desktop_printer_name or "").strip()
    workstation = _order_workstation(label.order_id, desktop=bool(desktop_agent_id))
    if workstation is None:
        if force:
            raise FbsLabelError(
                "У волны нет рабочего места."
                if desktop_agent_id
                else "У волны нет рабочего места с принтером и агентом печати."
            )
        return None
    if desktop_agent_id and not desktop_printer_name:
        raise FbsLabelError("В Fullbox Desktop не выбран принтер для этого задания.")
    content = _render_preloaded_ozon_order_label(
        barcode=label.barcode,
        external_order_id=label.order.external_order_id,
    )
    key = f"fbs:ozon-order-qr:{label.id}:{sha256(label.barcode.encode()).hexdigest()[:16]}"
    return _queue_png(
        workstation=workstation,
        card_id=key,
        order_id=f"FBS-OZON-ORDER-{label.order_id}",
        article=label.order.external_order_id,
        barcode=label.barcode,
        content=content,
        actor=actor,
        deduplicate=not force,
        label_width_mm=FBS_LABEL_WIDTH_MM,
        label_height_mm=FBS_LABEL_HEIGHT_MM,
        desktop_agent_id=desktop_agent_id,
        desktop_printer_name=desktop_printer_name,
    )


@transaction.atomic
def queue_fbs_handover_box_label_print(
    *,
    batch_id: int,
    box_id: int,
    workstation_id: int,
    requested_by,
    desktop_agent_id: str = "",
    desktop_printer_name: str = "",
) -> ProcessingPrintJob:
    _require_printing_enabled()
    actor = _print_actor(requested_by, required=True)
    workstation = _configured_workstation(
        workstation_id,
        desktop_agent_id=desktop_agent_id,
        desktop_printer_name=desktop_printer_name,
    )
    box = (
        FbsHandoverBox.objects.select_for_update()
        .select_related("batch")
        .filter(pk=box_id, batch_id=batch_id)
        .first()
    )
    if box is None:
        raise FbsLabelError("QR короба не относится к этой поставке.")
    content = _read_png(box.label_file)
    return _queue_png(
        workstation=workstation,
        card_id=f"fbs:handover-box:{box.id}",
        order_id=f"FBS-HANDOVER-{box.batch_id}",
        article=box.external_box_id or f"Короб {box.id}",
        barcode=box.qr_code,
        content=content,
        actor=actor,
        deduplicate=False,
        desktop_agent_id=desktop_agent_id,
        desktop_printer_name=desktop_printer_name,
    )


@transaction.atomic
def queue_fbs_handover_supply_label_print(
    *,
    batch_id: int,
    workstation_id: int,
    requested_by,
    desktop_agent_id: str = "",
    desktop_printer_name: str = "",
) -> ProcessingPrintJob:
    _require_printing_enabled()
    actor = _print_actor(requested_by, required=True)
    batch = (
        FbsHandoverBatch.objects.select_for_update()
        .select_related("profile__agency")
        .filter(pk=batch_id)
        .first()
    )
    if batch is None:
        raise FbsLabelError("Поставка FBS не найдена.")
    if batch.status in {
        FbsHandoverBatch.STATUS_OPEN,
        FbsHandoverBatch.STATUS_READY,
    }:
        from .handover import assert_handover_composition_ready

        assert_handover_composition_ready(batch)
    workstation = _configured_workstation(
        workstation_id,
        desktop_agent_id=desktop_agent_id,
        desktop_printer_name=desktop_printer_name,
    )
    is_ozon = batch.profile.marketplace == FbsIntegrationProfile.MARKETPLACE_OZON
    content = (
        render_ozon_handover_internal_label(batch)
        if is_ozon
        else _read_png(batch.supply_label_file)
    )
    barcode = f"FBS-HANDOVER-{batch.id}" if is_ozon else batch.supply_qr_code
    return _queue_png(
        workstation=workstation,
        card_id=f"fbs:handover-supply:{batch.id}",
        order_id=f"FBS-HANDOVER-{batch.id}",
        article=batch.external_supply_id or f"Отгрузка {batch.id}",
        barcode=barcode,
        content=content,
        actor=actor,
        deduplicate=False,
        desktop_agent_id=desktop_agent_id,
        desktop_printer_name=desktop_printer_name,
    )
