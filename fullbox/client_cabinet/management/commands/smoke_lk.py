from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from django.core.management.base import BaseCommand, CommandError
from django.test import RequestFactory
from django.urls import resolve

from employees.models import Employee
from sku.models import Agency


class SmokeFailure(Exception):
    pass


REQUEST_REQUIRED_FIELDS = (
    "number",
    "type",
    "status",
    "status_pill",
    "bucket",
    "cancel_policy",
    "manager_status_label",
)
REQUEST_STATUSES = {"waiting", "processing", "clarification", "completed", "cancelled"}


class Command(BaseCommand):
    help = "Read-only smoke-check for client LK and manager LK pages."

    def add_arguments(self, parser):
        parser.add_argument("--client-id", type=int, help="Agency id to check.")
        parser.add_argument("--host", default="lk.fullbox.ru", help="HTTP host for request resolving.")
        parser.add_argument(
            "--max-client-page-ms",
            type=int,
            default=15000,
            help="Fail if client LK main page is slower than this limit. Use 0 to disable.",
        )
        parser.add_argument(
            "--max-client-api-ms",
            type=int,
            default=10000,
            help="Fail if a client LK API endpoint is slower than this limit. Use 0 to disable.",
        )
        parser.add_argument(
            "--max-manager-page-ms",
            type=int,
            default=5000,
            help="Fail if manager/head-manager LK page is slower than this limit. Use 0 to disable.",
        )
        parser.add_argument(
            "--skip-managers",
            action="store_true",
            help="Check only client LK endpoints.",
        )

    def handle(self, *args, **options):
        self.factory = RequestFactory()
        self.host = options["host"]
        self.max_client_page_ms = int(options.get("max_client_page_ms") or 0)
        self.max_client_api_ms = int(options.get("max_client_api_ms") or 0)
        self.max_manager_page_ms = int(options.get("max_manager_page_ms") or 0)
        failures: list[str] = []

        agency = self._resolve_agency(options.get("client_id"))
        try:
            self._check_client_lk(agency)
        except SmokeFailure as exc:
            failures.append(str(exc))

        if not options.get("skip_managers"):
            for role, path in (("manager", "/team-manager/"), ("head_manager", "/head-manager/")):
                try:
                    self._check_role_page(role=role, path=path)
                except SmokeFailure as exc:
                    failures.append(str(exc))

        if failures:
            for failure in failures:
                self.stdout.write(self.style.ERROR(f"FAIL {failure}"))
            raise CommandError(f"LK smoke failed: {len(failures)} failure(s)")

        self.stdout.write(self.style.SUCCESS("OK LK smoke passed"))

    def _resolve_agency(self, client_id: int | None) -> Agency:
        qs = Agency.objects.select_related("portal_user")
        if client_id:
            agency = qs.filter(id=client_id).first()
            if not agency:
                raise CommandError(f"Client {client_id} not found")
        else:
            agency = qs.filter(portal_user__isnull=False).order_by("id").first()
            if not agency:
                raise CommandError("No client with portal_user found")
        if not getattr(agency, "portal_user_id", None):
            raise CommandError(f"Client {agency.id} has no portal_user")
        return agency

    def _response_for(self, path: str, user):
        parsed = urlsplit(path)
        request = self.factory.get(path, HTTP_HOST=self.host)
        request.user = user
        request.session = {}
        match = resolve(parsed.path)
        response = match.func(request, *match.args, **match.kwargs)
        if hasattr(response, "render"):
            response = response.render()
        return response

    def _timed_response_for(self, path: str, user, *, max_ms: int = 0, label: str = ""):
        started = time.perf_counter()
        response = self._response_for(path, user)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if max_ms and duration_ms > max_ms:
            name = label or path
            raise SmokeFailure(f"{name} too slow: {duration_ms}ms > {max_ms}ms")
        return response, duration_ms

    def _json_for(self, path: str, user) -> dict:
        response = self._response_for(path, user)
        if response.status_code != 200:
            body = response.content[:300].decode("utf-8", "replace") if getattr(response, "content", None) else ""
            raise SmokeFailure(f"{path} returned {response.status_code}: {body}")
        try:
            return json.loads(response.content.decode("utf-8"))
        except Exception as exc:
            raise SmokeFailure(f"{path} returned invalid JSON: {exc}") from exc

    def _timed_json_for(self, path: str, user, *, max_ms: int = 0, label: str = "") -> tuple[dict, int]:
        started = time.perf_counter()
        payload = self._json_for(path, user)
        duration_ms = int((time.perf_counter() - started) * 1000)
        if max_ms and duration_ms > max_ms:
            name = label or path
            raise SmokeFailure(f"{name} too slow: {duration_ms}ms > {max_ms}ms")
        return payload, duration_ms

    def _check_client_lk(self, agency: Agency):
        user = agency.portal_user
        page_path = f"/client/dashboard/lk/?client={agency.id}"
        page, page_ms = self._timed_response_for(
            page_path,
            user,
            max_ms=self.max_client_page_ms,
            label="client LK main page",
        )
        if page.status_code != 200:
            raise SmokeFailure(f"{page_path} returned {page.status_code}")
        page_body = page.content.decode("utf-8", "replace")
        if "WMS - LIVE" not in page_body and "Ваши площадки" not in page_body:
            raise SmokeFailure(f"{page_path} does not look like client LK")

        dashboard, dashboard_ms = self._timed_json_for(
            f"/client/api/v1/dashboard/?client={agency.id}",
            user,
            max_ms=self.max_client_api_ms,
            label="client dashboard API",
        )
        journal, stock_journal_ms = self._timed_json_for(
            f"/client/api/v1/stock-journal/?client={agency.id}&hide_consumed=1",
            user,
            max_ms=self.max_client_api_ms,
            label="client stock journal API",
        )
        if not dashboard.get("ok"):
            raise SmokeFailure("dashboard API returned ok=false")
        if not journal.get("ok"):
            raise SmokeFailure("stock-journal API returned ok=false")

        stock = dashboard["data"]["stock"]["summary"]
        products = dashboard["data"]["stock"].get("all_products") or []
        journal_summary = journal["data"]["summary"]
        visible_total = int(stock.get("total_units") or 0)
        visible_available = int(stock.get("available") or 0)
        journal_available = int(journal_summary.get("available_qty") or 0)
        if visible_total != journal_available:
            raise SmokeFailure(
                "visible stock total mismatch: "
                f"dashboard={stock.get('total_units')} journal_available={journal_summary.get('available_qty')}"
            )
        if visible_available != journal_available:
            raise SmokeFailure(
                "available stock mismatch: "
                f"dashboard={stock.get('available')} journal={journal_summary.get('available_qty')}"
            )
        if visible_available != visible_total:
            raise SmokeFailure(
                "client visible total must equal available stock: "
                f"available={stock.get('available')} total={stock.get('total_units')}"
            )
        product_total = sum(int(item.get("qty") or 0) for item in products)
        product_available = sum(int(item.get("available_qty") or 0) for item in products)
        if product_total and product_total != visible_total:
            raise SmokeFailure(
                "product stock total mismatch: "
                f"products={product_total} summary={stock.get('total_units')}"
            )
        if product_total and product_available != visible_available:
            raise SmokeFailure(
                "product available stock mismatch: "
                f"products={product_available} summary={stock.get('available')}"
            )

        requests = dashboard["data"].get("requests") or []
        self._check_request_rows(requests)

        self.stdout.write(
            self.style.SUCCESS(
                "OK client "
                f"{agency.id}: total={stock.get('total_units')} "
                f"available={stock.get('available')} requests={len(requests)} "
                f"page_ms={page_ms} dashboard_api_ms={dashboard_ms} "
                f"stock_journal_api_ms={stock_journal_ms}"
            )
        )

    def _check_request_rows(self, rows: list[dict]):
        for idx, row in enumerate(rows, start=1):
            missing = [field for field in REQUEST_REQUIRED_FIELDS if row.get(field) in (None, "")]
            if missing:
                number = row.get("number") or row.get("order_id") or f"row#{idx}"
                raise SmokeFailure(f"request {number} has missing LK fields: {', '.join(missing)}")
            status = row.get("status")
            if status not in REQUEST_STATUSES:
                number = row.get("number") or row.get("order_id") or f"row#{idx}"
                raise SmokeFailure(f"request {number} has unknown LK status: {status}")
            if row.get("cancel_policy") not in {"direct", "manager_approval", "none"}:
                number = row.get("number") or row.get("order_id") or f"row#{idx}"
                raise SmokeFailure(f"request {number} has unknown cancel policy: {row.get('cancel_policy')}")

    def _check_role_page(self, *, role: str, path: str):
        employee = (
            Employee.objects.filter(role=role, is_active=True, user__isnull=False)
            .select_related("user")
            .order_by("id")
            .first()
        )
        if not employee:
            raise SmokeFailure(f"no active {role} employee with user")
        response, page_ms = self._timed_response_for(
            path,
            employee.user,
            max_ms=self.max_manager_page_ms,
            label=f"{role} LK page",
        )
        if response.status_code != 200:
            raise SmokeFailure(f"{path} for {role} returned {response.status_code}")
        body = response.content.decode("utf-8", "replace")
        if "Задачи" not in body and "task-filter" not in body:
            raise SmokeFailure(f"{path} for {role} does not look like manager LK")
        self.stdout.write(self.style.SUCCESS(f"OK {role}: {path} page_ms={page_ms}"))
