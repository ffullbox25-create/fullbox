"""Рабочие read-only отчёты ЛК менеджера и их выгрузки."""

from __future__ import annotations

from datetime import timedelta
from dataclasses import asdict
from urllib.parse import urlencode

from django.core.files.base import ContentFile
from django.db import DatabaseError
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from accountant.selectors import manager_visible_agencies
from employees.access import RoleRequiredMixin, get_request_role
from sku.models import SKU

from .models import TeamReportExportJob, TeamReportFavorite, TeamSavedReport
from .decision_reports import (
    build_decision_report,
    decision_report_available,
    display_decision_cell,
    export_decision_report_response,
)
from .product_reports import build_product_report, display_cell, export_product_report_response, product_report_available
from .report_catalog import (
    active_filters_from_params,
    get_report,
    get_section,
    global_filter_query,
    report_to_dict,
    search_reports,
    sections_for_role,
)
from .roles import CABINET_ROLES


PRODUCT_MOVEMENT_PRIMARY_FILTER_CODES = frozenset(
    {
        "date_from",
        "date_to",
        "client_id",
        "product",
        "article",
        "barcode",
    }
)
PRODUCT_ARTICLE_OPTION_LIMIT = 100


def _safe_favorite_rows(user):
    if not getattr(user, "is_authenticated", False):
        return []
    try:
        return list(TeamReportFavorite.objects.filter(user=user))
    except DatabaseError:
        # Если код уже выложен, а миграция ещё не применена, страница не должна падать.
        return []


def _safe_saved_rows(user):
    if not getattr(user, "is_authenticated", False):
        return []
    try:
        return list(TeamSavedReport.objects.filter(user=user)[:50])
    except DatabaseError:
        return []


def _safe_job_rows(user):
    if not getattr(user, "is_authenticated", False):
        return []
    try:
        return list(TeamReportExportJob.objects.filter(user=user)[:50])
    except DatabaseError:
        return []


def _favorite_key_set(user) -> set[tuple[str, str]]:
    return {(row.section, row.report_code) for row in _safe_favorite_rows(user)}


def _serialize_report(report, favorite_keys: set[tuple[str, str]], *, query_string: str = "") -> dict:
    data = report_to_dict(report, is_favorite=(report.section, report.code) in favorite_keys)
    if query_string:
        data["url_with_filters"] = f"{data['url']}?{query_string}"
    else:
        data["url_with_filters"] = data["url"]
    return data


def _serialize_section(section, favorite_keys: set[tuple[str, str]], *, query_string: str = "") -> dict:
    favorite_count = sum(1 for report in section.reports if (report.section, report.code) in favorite_keys)
    return {
        "key": section.key,
        "title": section.title,
        "description": section.description,
        "icon": section.icon,
        "url": f"{section.url}?{query_string}" if query_string else section.url,
        "report_count": len(section.reports),
        "favorite_count": favorite_count,
        "reports": [_serialize_report(report, favorite_keys, query_string=query_string) for report in section.reports],
    }


def _report_payload(report) -> dict:
    return {
        "code": report.code,
        "section": report.section,
        "title": report.title,
        "description": report.description,
        "keywords": list(report.keywords),
        "filters": [asdict(item) for item in report.filters],
        "columns": [asdict(item) for item in report.columns],
        "export_formats": list(report.export_formats),
        "status": report.status,
        "url": report.url,
    }


def _product_balance_client_options() -> list[dict]:
    options = []
    agencies = manager_visible_agencies().order_by("agn_name", "id").values("id", "agn_name", "short_name", "inn")
    for agency in agencies:
        name = str(agency.get("short_name") or agency.get("agn_name") or f"Клиент {agency['id']}").strip()
        inn = str(agency.get("inn") or "").strip()
        options.append({"value": str(agency["id"]), "label": f"{name} · ИНН {inn}" if inn else name})
    return options


class TeamManagerReportsBase(RoleRequiredMixin):
    allowed_roles = CABINET_ROLES

    def _role(self):
        return get_request_role(self.request)

    def _common_context(self) -> dict:
        query = self.request.GET.get("q", "").strip()
        query_string = global_filter_query(self.request.GET)
        favorite_keys = _favorite_key_set(self.request.user)
        sections = sections_for_role(self._role())
        favorite_reports = [
            _serialize_report(report, favorite_keys, query_string=query_string)
            for report in search_reports("", self._role())
            if (report.section, report.code) in favorite_keys
        ]
        return {
            "active_nav": "reports",
            "page_query": query,
            "global_filter_query": query_string,
            "global_filters": {
                "period": self.request.GET.get("period", ""),
                "date": self.request.GET.get("date", ""),
                "date_from": self.request.GET.get("date_from", ""),
                "date_to": self.request.GET.get("date_to", ""),
                "client_id": self.request.GET.get("client_id", ""),
                "warehouse_id": self.request.GET.get("warehouse_id", ""),
                "operation_type": self.request.GET.get("operation_type", ""),
                "legal_entity": self.request.GET.get("legal_entity", ""),
            },
            "active_filters": active_filters_from_params(self.request.GET),
            "favorite_reports": favorite_reports,
            "saved_reports": _safe_saved_rows(self.request.user),
            "recent_jobs": _safe_job_rows(self.request.user)[:5],
            "report_sections": [
                _serialize_section(section, favorite_keys, query_string=query_string)
                for section in sections
            ],
            "report_total_count": sum(len(section.reports) for section in sections),
        }


class TeamManagerProductArticlesApi(TeamManagerReportsBase, View):
    """Артикулы активного клиента для зависимого фильтра товарного отчёта."""

    def get(self, request):
        try:
            client_id = int(str(request.GET.get("client_id") or "").strip())
        except (TypeError, ValueError):
            return JsonResponse({"ok": False, "error": "Выберите клиента."}, status=400)
        if not manager_visible_agencies().filter(pk=client_id).exists():
            return JsonResponse({"ok": False, "error": "Клиент не найден."}, status=404)

        query = str(request.GET.get("q") or "").strip()[:80]
        articles = SKU.objects.filter(agency_id=client_id, deleted=False).exclude(sku_code="")
        if query:
            articles = articles.filter(
                Q(sku_code__icontains=query)
                | Q(name__icontains=query)
                | Q(code__icontains=query)
            )
        articles = articles.order_by("sku_code", "id")
        total = articles.count()
        rows = list(articles.values("sku_code", "name")[:PRODUCT_ARTICLE_OPTION_LIMIT])
        options = []
        for row in rows:
            article = str(row.get("sku_code") or "").strip()
            name = str(row.get("name") or "").strip()
            options.append(
                {
                    "value": article,
                    "label": f"{article} — {name}" if name and name != article else article,
                }
            )
        return JsonResponse(
            {
                "ok": True,
                "client_id": client_id,
                "query": query,
                "total": total,
                "truncated": total > len(options),
                "options": options,
            }
        )


class TeamManagerReportsView(TeamManagerReportsBase, TemplateView):
    template_name = "teammanager/reports.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        common = self._common_context()
        favorite_keys = _favorite_key_set(self.request.user)
        found = search_reports(common["page_query"], self._role())
        ctx.update(common)
        featured_paths = (
            ("warehouse", "current-stock"),
            ("warehouse", "warehouse-operation-history"),
            ("operations", "warehouse-task-queue"),
            ("clients", "client-summary"),
            ("finance", "client-debt"),
            ("nomenclature", "without-barcode"),
        )
        ctx.update(
            {
                "search_results": [
                    _serialize_report(report, favorite_keys, query_string=common["global_filter_query"])
                    for report in found[:40]
                ],
                "search_results_count": len(found),
                "featured_reports": [
                    _serialize_report(report, favorite_keys)
                    for section, code in featured_paths
                    if (report := get_report(section, code, self._role())) is not None
                ],
            }
        )
        return ctx


class TeamManagerReportSectionView(TeamManagerReportsBase, TemplateView):
    template_name = "teammanager/reports_section.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        section = get_section(self.kwargs["section"], self._role())
        if section is None:
            raise Http404("Раздел отчётов не найден")
        common = self._common_context()
        favorite_keys = _favorite_key_set(self.request.user)
        found = search_reports(common["page_query"], self._role(), section.key)
        ctx.update(common)
        ctx.update(
            {
                "section": _serialize_section(section, favorite_keys, query_string=common["global_filter_query"]),
                "section_reports": [
                    _serialize_report(report, favorite_keys, query_string=common["global_filter_query"])
                    for report in found
                ],
                "section_search_results_count": len(found),
                "section_favorites": [
                    _serialize_report(report, favorite_keys, query_string=common["global_filter_query"])
                    for report in section.reports
                    if (report.section, report.code) in favorite_keys
                ],
            }
        )
        return ctx


class TeamManagerReportDetailView(TeamManagerReportsBase, TemplateView):
    template_name = "teammanager/reports_detail.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        report = get_report(self.kwargs["section"], self.kwargs["report_code"], self._role())
        if report is None:
            raise Http404("Отчёт не найден")
        section = get_section(report.section, self._role())
        favorite_keys = _favorite_key_set(self.request.user)
        common = self._common_context()
        query_items = [
            (key, value)
            for key, value in self.request.GET.items()
            if value not in ("", "all", "0")
        ]
        query_without_page = self.request.GET.copy()
        query_without_page.pop("page", None)
        query_without_page = query_without_page.urlencode()
        compact_report = report.code == "product-movement"
        client_options = (
            _product_balance_client_options()
            if any(filter_spec.code == "client_id" for filter_spec in report.filters)
            else []
        )
        report_filter_fields = []
        for filter_spec in report.filters:
            item = asdict(filter_spec)
            item["value"] = self.request.GET.get(filter_spec.code, "")
            item["checked"] = self.request.GET.get(filter_spec.code) in {"1", "true", "on", "yes"}
            item["options"] = client_options if filter_spec.code == "client_id" and client_options else []
            item["dependent_options"] = compact_report and filter_spec.code == "article"
            report_filter_fields.append(item)
        if compact_report:
            primary_report_filters = [
                item for item in report_filter_fields if item["code"] in PRODUCT_MOVEMENT_PRIMARY_FILTER_CODES
            ]
            advanced_report_filters = [
                item for item in report_filter_fields if item["code"] not in PRODUCT_MOVEMENT_PRIMARY_FILTER_CODES
            ]
        else:
            primary_report_filters = report_filter_fields
            advanced_report_filters = []
        selected_page_size = self.request.GET.get("page_size", "50")
        selected_sort = self.request.GET.get("sort", "")
        advanced_filters_open = compact_report and (
            any(item["checked"] or str(item["value"] or "").strip() for item in advanced_report_filters)
            or selected_page_size != "50"
            or bool(selected_sort)
        )
        report_data = None
        table_rows = []
        total_cells = []
        cell_display = display_cell
        if product_report_available(report.code) and report.section == "products":
            report_data = build_product_report(report, self.request.GET, user=self.request.user)
        elif decision_report_available(report):
            report_data = build_decision_report(report, self.request.GET, user=self.request.user)
            cell_display = display_decision_cell
        if report_data is not None:
            table_rows = [
                {
                    "values": [
                        {
                            "code": column.code,
                            "value": cell_display(row, column.code),
                        }
                        for column in report_data.visible_columns
                    ],
                }
                for row in report_data.page_rows
            ]
            for index, column in enumerate(report_data.visible_columns):
                if index == 0:
                    total_cells.append("Итого")
                else:
                    total_cells.append(cell_display(report_data.totals, column.code) if column.code in report_data.totals else "—")
            try:
                TeamReportFavorite.objects.filter(
                    user=self.request.user,
                    section=report.section,
                    report_code=report.code,
                ).update(last_filters=dict(self.request.GET.items()), last_generated_at=timezone.now())
            except DatabaseError:
                pass
        ctx.update(common)
        ctx.update(
            {
                "section": _serialize_section(section, favorite_keys, query_string=common["global_filter_query"]) if section else None,
                "report": _serialize_report(report, favorite_keys, query_string=common["global_filter_query"]),
                "report_filters": report_filter_fields,
                "primary_report_filters": primary_report_filters,
                "advanced_report_filters": advanced_report_filters,
                "compact_report": compact_report,
                "advanced_filters_open": advanced_filters_open,
                "selected_product_client_id": self.request.GET.get("client_id", "") if compact_report else "",
                "product_article_options_url": reverse("team-manager-product-articles-api") if compact_report else "",
                "report_columns": report_data.visible_columns if report_data else report.columns,
                "hidden_report_columns": report_data.hidden_columns if report_data else [],
                "visible_column_codes": [column.code for column in (report_data.visible_columns if report_data else report.columns)],
                "current_query_items": query_items,
                "generated_at": report_data.generated_at if report_data else timezone.localtime(),
                "page_size_options": (20, 50, 100, 200),
                "selected_page_size": selected_page_size,
                "selected_sort": selected_sort,
                "query_without_page": query_without_page,
                "report_data": report_data,
                "table_rows": table_rows,
                "total_cells": total_cells,
            }
        )
        return ctx


class TeamManagerReportsHistoryView(TeamManagerReportsBase, TemplateView):
    template_name = "teammanager/reports_history.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(self._common_context())
        ctx["export_jobs"] = _safe_job_rows(self.request.user)
        return ctx


class TeamManagerSavedReportsView(TeamManagerReportsBase, TemplateView):
    template_name = "teammanager/reports_saved.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(self._common_context())
        saved_reports = _safe_saved_rows(self.request.user)
        saved_report_cards = []
        for saved in saved_reports:
            query = {}
            if isinstance(saved.filters, dict):
                query.update({key: value for key, value in saved.filters.items() if value not in ("", "all", "0", None)})
            if isinstance(saved.sorting, dict) and saved.sorting.get("sort"):
                query["sort"] = saved.sorting["sort"]
            if saved.visible_columns:
                query["columns"] = saved.visible_columns
            url = f"/team-manager/reports/{saved.section}/{saved.report_code}/"
            if query:
                url = f"{url}?{urlencode(query, doseq=True)}"
            saved_report_cards.append({"row": saved, "url": url})
        ctx["saved_reports"] = saved_reports
        ctx["saved_report_cards"] = saved_report_cards
        return ctx


class TeamManagerReportFavoriteToggleView(TeamManagerReportsBase, View):
    def post(self, request, section, report_code):
        report = get_report(section, report_code, get_request_role(request))
        if report is None:
            raise Http404("Отчёт не найден")
        try:
            favorite = TeamReportFavorite.objects.filter(
                user=request.user,
                section=section,
                report_code=report_code,
            ).first()
            if favorite:
                favorite.delete()
            else:
                TeamReportFavorite.objects.create(
                    user=request.user,
                    section=section,
                    report_code=report_code,
                    last_filters=dict(request.GET.items()),
                )
        except DatabaseError:
            pass
        next_url = request.POST.get("next") or report.url
        return redirect(next_url)


class TeamManagerSavedReportCreateView(TeamManagerReportsBase, View):
    def post(self, request, section, report_code):
        report = get_report(section, report_code, get_request_role(request))
        if report is None:
            raise Http404("Отчёт не найден")
        filters = {
            key.replace("filter_", "", 1): value
            for key, value in request.POST.items()
            if key.startswith("filter_") and value not in ("", "all", "0")
        }
        title = (request.POST.get("title") or "").strip() or f"{report.title} · {timezone.localtime():%d.%m.%Y %H:%M}"
        try:
            TeamSavedReport.objects.create(
                user=request.user,
                title=title[:160],
                section=section,
                report_code=report_code,
                filters=filters,
                sorting={"sort": request.POST.get("filter_sort", "")} if request.POST.get("filter_sort") else {},
                visible_columns=request.POST.getlist("filter_columns"),
                export_format=(request.POST.get("export_format") or "xlsx").lower(),
            )
        except DatabaseError:
            pass
        return redirect(reverse("team-manager-saved-reports"))


class TeamManagerReportCatalogApi(TeamManagerReportsBase, View):
    def get(self, request):
        role = get_request_role(request)
        sections = sections_for_role(role)
        return JsonResponse(
            {
                "sections": [
                    {
                        "key": section.key,
                        "title": section.title,
                        "description": section.description,
                        "report_count": len(section.reports),
                        "url": section.url,
                    }
                    for section in sections
                ],
                "total_reports": sum(len(section.reports) for section in sections),
            }
        )


class TeamManagerReportSectionApi(TeamManagerReportsBase, View):
    def get(self, request, section):
        section_obj = get_section(section, get_request_role(request))
        if section_obj is None:
            raise Http404("Раздел отчётов не найден")
        return JsonResponse(
            {
                "section": {
                    "key": section_obj.key,
                    "title": section_obj.title,
                    "description": section_obj.description,
                    "report_count": len(section_obj.reports),
                    "url": section_obj.url,
                },
                "reports": [_report_payload(report) for report in section_obj.reports],
            }
        )


class TeamManagerReportMetadataApi(TeamManagerReportsBase, View):
    def get(self, request, section, report_code):
        report = get_report(section, report_code, get_request_role(request))
        if report is None:
            raise Http404("Отчёт не найден")
        return JsonResponse({"report": _report_payload(report)})


class TeamManagerReportExportView(TeamManagerReportsBase, View):
    def get(self, request, section, report_code, export_format):
        export_format = str(export_format or "").lower()
        if export_format not in {"xlsx", "csv"}:
            raise Http404("Формат выгрузки не найден")
        report = get_report(section, report_code, get_request_role(request))
        if report is None:
            raise Http404("Отчёт не найден")
        is_product = report.section == "products" and product_report_available(report.code)
        is_decision = decision_report_available(report)
        if not (is_product or is_decision):
            raise Http404("Выгрузка для отчёта пока не подключена")
        filters = {key: value for key, value in request.GET.items() if value not in ("", "all", "0")}
        job = None
        try:
            job = TeamReportExportJob.objects.create(
                user=request.user,
                section=section,
                report_code=report_code,
                report_title=report.title[:160],
                filters=filters,
                export_format=export_format,
                status=TeamReportExportJob.STATUS_BUILDING,
                started_at=timezone.now(),
            )
        except DatabaseError:
            job = None
        try:
            exporter = export_product_report_response if is_product else export_decision_report_response
            response, row_count, payload, filename = exporter(report, request.GET, export_format, user=request.user)
            if job is not None:
                finished_at = timezone.now()
                job.row_count = row_count
                job.status = TeamReportExportJob.STATUS_READY
                job.finished_at = finished_at
                job.expires_at = finished_at + timedelta(days=14)
                job.file.save(filename, ContentFile(payload), save=False)
                job.save(update_fields=["row_count", "status", "finished_at", "expires_at", "file", "updated_at"])
            return response
        except Exception as exc:  # pragma: no cover - защитный контур для истории ошибок
            if job is not None:
                job.status = TeamReportExportJob.STATUS_ERROR
                job.error_text = str(exc)
                job.finished_at = timezone.now()
                job.save(update_fields=["status", "error_text", "finished_at", "updated_at"])
            return JsonResponse({"ok": False, "error": "Не удалось сформировать выгрузку"}, status=500)
