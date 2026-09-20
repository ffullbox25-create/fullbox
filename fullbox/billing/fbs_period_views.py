from __future__ import annotations

import re
from urllib.parse import quote, urlencode

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.dateparse import parse_date

from sku.models import Agency

from .permissions import filter_agencies_for_user, get_billing_role
from .views import BillingBaseView


def _fbs_period_report_filename(*, company_name: str, date_from, date_to) -> str:
    """Build a readable, filesystem-safe UTF-8 name for the exported workbook."""
    safe_company = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(company_name or ""))
    safe_company = "_".join(safe_company.split()).strip(" ._")[:100]
    safe_company = safe_company or "КЛИЕНТ"
    return f"{safe_company}_FBS_{date_from:%Y-%m-%d}_{date_to:%Y-%m-%d}.xlsx"


def _fbs_period_content_disposition(*, filename: str, date_from, date_to) -> str:
    fallback = f"FBS_{date_from:%Y-%m-%d}_{date_to:%Y-%m-%d}.xlsx"
    encoded_filename = quote(filename, safe="")
    return (
        f'attachment; filename="{fallback}"; '
        f"filename*=UTF-8''{encoded_filename}"
    )


class BillingFbsPeriodReportView(BillingBaseView):
    """Download a current FBS period report without creating an invoice."""

    billing_section = "fbs"
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        if get_billing_role(request) not in {"accountant", "admin", "director"}:
            raise PermissionDenied("Формировать отчёт FBS может бухгалтер.")

        client_id = str(request.POST.get("client") or "").strip()
        raw_from = str(request.POST.get("date_from") or "").strip()
        raw_to = str(request.POST.get("date_to") or "").strip()
        query = {"client": client_id, "date_from": raw_from, "date_to": raw_to}
        return_url = f"/team-manager/billing/fbs/?{urlencode(query)}"
        try:
            if not client_id.isdigit():
                raise ValidationError("Выберите клиента для отчёта FBS.")
            date_from = parse_date(raw_from)
            date_to = parse_date(raw_to)
            if not date_from or not date_to or date_from > date_to:
                raise ValidationError("Укажите корректный период отчёта FBS.")
            client = get_object_or_404(
                filter_agencies_for_user(Agency.objects.all(), request),
                pk=int(client_id),
            )

            from .fbs_period_billing import generate_period_report

            result = generate_period_report(
                client=client,
                date_from=date_from,
                date_to=date_to,
            )
        except (ValidationError, ValueError) as exc:
            message = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            messages.error(request, message)
            return redirect(return_url)

        response = HttpResponse(
            result["workbook"],
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        filename = _fbs_period_report_filename(
            company_name=client.agn_name,
            date_from=date_from,
            date_to=date_to,
        )
        response["Content-Disposition"] = _fbs_period_content_disposition(
            filename=filename,
            date_from=date_from,
            date_to=date_to,
        )
        return response


class BillingFbsPeriodInvoiceView(BillingBaseView):
    """Create the FBS period invoice separately from the downloadable report."""

    billing_section = "fbs"
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        if get_billing_role(request) not in {"accountant", "admin", "director"}:
            raise PermissionDenied("Формировать счёт FBS может бухгалтер.")

        client_id = str(request.POST.get("client") or "").strip()
        raw_from = str(request.POST.get("date_from") or "").strip()
        raw_to = str(request.POST.get("date_to") or "").strip()
        query = {"client": client_id, "date_from": raw_from, "date_to": raw_to}
        return_url = f"/team-manager/billing/fbs/?{urlencode(query)}"
        try:
            if not client_id.isdigit():
                raise ValidationError("Выберите клиента для счёта FBS.")
            date_from = parse_date(raw_from)
            date_to = parse_date(raw_to)
            if not date_from or not date_to or date_from > date_to:
                raise ValidationError("Укажите корректный период счёта FBS.")
            client = get_object_or_404(
                filter_agencies_for_user(Agency.objects.all(), request),
                pk=int(client_id),
            )

            from .fbs_period_billing import generate_period_invoice

            result = generate_period_invoice(
                client=client,
                date_from=date_from,
                date_to=date_to,
                user=request.user,
            )
        except (ValidationError, ValueError) as exc:
            message = "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            messages.error(request, message)
            return redirect(return_url)

        invoice = result["invoice"]
        if result["invoice_created"]:
            messages.success(
                request,
                f"Счёт {invoice.number} создан по расчёту FBS за выбранный период.",
            )
        else:
            messages.info(
                request,
                f"Счёт {invoice.number} за выбранный период уже существует; дубликат не создан.",
            )
        return redirect(return_url)
