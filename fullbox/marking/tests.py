import base64
import json
import os
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase
from django.utils import timezone
from openpyxl import Workbook

from audit.models import AuditEntry, OrderAuditEntry
from agent.models import DeviceAgent
from employees.models import Employee
from sku.models import Agency, SKU, SKUBarcode
from processing_app.models import ProcessingPrintJob
from processing_app.services import ProcessingWorkflowService

from .models import MarkingCode
from .services import (
    _free_print_job_ids,
    InvalidHonestSignCode,
    TrueApiNotConfigured,
    check_honest_sign_status,
    free_marking_batch_status_response,
    honest_sign_duplicate_print_response,
    honest_sign_duplicate_validate_response,
    honest_sign_status_check_response,
    free_marking_candidates_response,
    free_marking_confirm_printed_response,
    free_marking_import_response,
    free_marking_queue_response,
    normalize_honest_sign_code,
    processing_marking_import_response,
    processing_marking_scan_response,
    processing_marking_summary_response,
    receiving_marking_scan_response,
    return_printed_marking_response,
)
from .views import (
    _duplicate_print_agents,
    free_marking_print_page,
    honest_sign_duplicate_page,
    honest_sign_status_page,
    return_printed_marking_page,
)


class MarkingServiceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username="marking_user", password="pwd")
        Employee.objects.create(
            user=self.user,
            full_name="Маркировщик",
            role="storekeeper",
            is_active=True,
        )
        self.head_user = get_user_model().objects.create_user(
            username="free_marking_head",
            password="pwd",
        )
        Employee.objects.create(
            user=self.head_user,
            full_name="Руководитель обработки",
            role="processing_head",
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент маркировки")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-1",
            name="Куртка",
            size="42",
            honest_sign=True,
        )
        SKUBarcode.objects.create(sku=self.sku, value="2000000001000", size="42")
        OrderAuditEntry.objects.create(
            order_id="PROC-1",
            order_type="processing",
            action="status",
            agency=self.agency,
            payload={
                "stock_rows": [
                    {
                        "article": "SKU-1",
                        "size": "42",
                        "barcode": "2000000001000",
                        "qty": 2,
                    }
                ]
            },
        )
        OrderAuditEntry.objects.create(
            order_id="REC-1",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "receiving_mode": "cz",
                "items": [
                    {
                        "sku_code": "SKU-1",
                        "size": "42",
                        "barcode": "2000000001000",
                        "qty": 2,
                    }
                ],
            },
        )

    def _post_json(self, path: str, payload: dict):
        request = self.factory.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
        )
        request.user = self.user
        return request

    def test_return_printed_marking_clears_only_print_state(self):
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021RETURN0001",
            source="import",
            created_by=self.head_user,
            printed_at=timezone.now(),
            printed_by=self.head_user,
        )
        request = self.factory.post(
            "/marking/return/scan/",
            data=json.dumps({"code": code.code}),
            content_type="application/json",
        )
        request.user = self.head_user

        response = return_printed_marking_response(request=request)
        payload = json.loads(response.content)
        code.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIsNone(code.printed_at)
        self.assertIsNone(code.printed_by)
        self.assertIsNone(code.print_job_id)
        self.assertIsNone(code.print_reserved_at)
        self.assertEqual(code.order_id, "PROC-1")
        self.assertIsNone(code.used_at)
        self.assertTrue(AuditEntry.objects.filter(journal__code="marking_return").exists())

    def test_return_accepts_completed_free_print_missing_browser_confirmation(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTED,
            order_id="PROC-1",
            article="SKU-1",
            barcode="2000000001000",
            processing_param_key="free_marking_print",
            printer_name="TSC TE200",
            agent="agent-1",
        )
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021RETURNPENDINGCONFIRM",
            source="import",
            created_by=self.head_user,
            printed_by=self.head_user,
            print_job_id=job.id,
            print_reserved_at=timezone.now(),
        )
        request = self._post_json("/marking/return/scan/", {"code": code.code})
        request.user = self.head_user

        response = return_printed_marking_response(request=request)
        payload = json.loads(response.content)
        code.refresh_from_db()
        job.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertIsNone(code.printed_at)
        self.assertIsNone(code.printed_by)
        self.assertIsNone(code.print_job_id)
        self.assertIsNone(code.print_reserved_at)
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_PRINTED)
        self.assertEqual(job.processing_param_key, "free_marking_returned")
        audit = AuditEntry.objects.filter(journal__code="marking_return").latest("id")
        self.assertTrue(audit.snapshot["print_completed_pending_confirmation"])

    def test_return_rejects_free_print_job_still_in_progress(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTING,
            order_id="PROC-1",
            article="SKU-1",
            barcode="2000000001000",
            processing_param_key="free_marking_print",
            printer_name="TSC TE200",
            agent="agent-1",
        )
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021RETURNPRINTING",
            source="import",
            created_by=self.head_user,
            printed_by=self.head_user,
            print_job_id=job.id,
            print_reserved_at=timezone.now(),
        )
        request = self._post_json("/marking/return/scan/", {"code": code.code})
        request.user = self.head_user

        response = return_printed_marking_response(request=request)
        payload = json.loads(response.content)
        code.refresh_from_db()

        self.assertEqual(response.status_code, 409)
        self.assertEqual(payload["error_code"], "print_in_progress")
        self.assertEqual(code.print_job_id, job.id)

    def test_return_page_accepts_fullbox_desktop_and_hid_scans_automatically(self):
        request = self.factory.get("/marking/return/")
        request.user = self.head_user

        response = return_printed_marking_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'window.addEventListener("fullbox:scan", handleDesktopScanEvent)')
        self.assertContains(response, 'input.addEventListener("input"')
        self.assertContains(response, "form.requestSubmit()")
        self.assertContains(response, "Сканирование происходит автоматически")

    def test_processing_marking_summary_counts_used_codes(self):
        MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            code="CZ-USED-1",
            used_at=OrderAuditEntry.objects.latest("id").created_at,
            used_by=self.user,
        )

        request = self.factory.get("/marking/processing/PROC-1/summary/")
        request.user = self.user
        response = processing_marking_summary_response(request=request, order_id="PROC-1")
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total_count"], 1)
        self.assertEqual(payload["items"][0]["sku_code"], "SKU-1")

    def test_processing_marking_scan_creates_used_code(self):
        request = self._post_json(
            "/marking/processing/PROC-1/scan/",
            {
                "code": "CZ-PROC-1",
                "sku_code": "SKU-1",
                "size": "42",
                "barcode": "2000000001000",
                "box_barcode": "BOX-1",
            },
        )

        response = processing_marking_scan_response(request=request, order_id="PROC-1")
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        code = MarkingCode.objects.get(code="CZ-PROC-1")
        self.assertEqual(code.order_type, "processing")
        self.assertEqual(code.order_id, "PROC-1")
        self.assertIsNotNone(code.used_at)

    def test_receiving_marking_scan_requires_open_box(self):
        request = self._post_json(
            "/marking/receiving/REC-1/scan/",
            {
                "code": "CZ-REC-1",
                "sku_code": "SKU-1",
                "size": "42",
                "barcode": "2000000001000",
            },
        )

        response = receiving_marking_scan_response(request=request, order_id="REC-1")
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error"], "Откройте короб перед сканированием ЧЗ.")

    def test_processing_marking_import_creates_codes_from_xlsx(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["barcode", "code"])
        sheet.append(["2000000001000", "CZ-IMP-1"])
        buffer = BytesIO()
        workbook.save(buffer)
        upload = SimpleUploadedFile(
            "codes.xlsx",
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        request = self.factory.post(
            "/marking/processing/PROC-1/import/",
            data={"file": upload},
        )
        request.user = self.user

        response = processing_marking_import_response(request=request, order_id="PROC-1")
        payload = json.loads(response.content)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["added"], 1)
        self.assertTrue(MarkingCode.objects.filter(code="CZ-IMP-1", order_id="PROC-1").exists())

    def test_honest_sign_normalization_removes_aim_and_crypto_tail(self):
        raw_code = "]d2010460000000000021SERIAL1234567\x1d91ABCD\x1d92CRYPTO"

        self.assertEqual(
            normalize_honest_sign_code(raw_code),
            "010460000000000021SERIAL1234567",
        )

    def test_honest_sign_normalization_rejects_non_gs1_code(self):
        with self.assertRaises(InvalidHonestSignCode):
            normalize_honest_sign_code("NOT-A-DATAMATRIX")

    @patch.dict(os.environ, {}, clear=False)
    def test_honest_sign_check_requires_true_api_token(self):
        os.environ.pop("CRPT_TRUE_API_TOKEN", None)
        os.environ.pop("CRPT_TRUE_API_TOKEN_FILE", None)

        with self.assertRaises(TrueApiNotConfigured):
            check_honest_sign_status("010460000000000021SERIAL1234567")

    @patch("marking.services.requests.post")
    @patch.dict(os.environ, {"CRPT_TRUE_API_TOKEN": "test-token"}, clear=False)
    def test_honest_sign_check_reports_code_in_circulation(self, post_mock):
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "cisInfo": {
                    "requestedCis": "010460000000000021SERIAL1234567",
                    "gtin": "04600000000000",
                    "productName": "Тестовый товар",
                    "status": "INTRODUCED",
                    "statusEx": "EMPTY",
                    "markWithdraw": False,
                    "expirationDate": "2099-01-01T00:00:00",
                }
            }
        ]
        post_mock.return_value = response

        result = check_honest_sign_status("010460000000000021SERIAL1234567")

        self.assertTrue(result["found"])
        self.assertTrue(result["accepted"])
        self.assertEqual(result["verdict"], "Код действующий: в обороте")
        self.assertEqual(result["crypto_check"], "Не выполнялась")
        post_mock.assert_called_once()

    @patch("marking.services.requests.post")
    @patch.dict(os.environ, {"CRPT_TRUE_API_TOKEN": "test-token"}, clear=False)
    def test_honest_sign_check_reports_retired_code(self, post_mock):
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "cisInfo": {
                    "requestedCis": "010460000000000021RETIRED123456",
                    "gtin": "04600000000000",
                    "status": "RETIRED",
                    "statusEx": "EMPTY",
                    "withdrawReason": "RETAIL",
                }
            }
        ]
        post_mock.return_value = response

        result = check_honest_sign_status("010460000000000021RETIRED123456")

        self.assertTrue(result["found"])
        self.assertFalse(result["accepted"])
        self.assertEqual(result["verdict"], "Выбыл из оборота")
        self.assertEqual(result["tone"], "danger")

    @patch("marking.services.requests.post")
    @patch.dict(os.environ, {"CRPT_TRUE_API_TOKEN": "test-token"}, clear=False)
    def test_honest_sign_endpoint_does_not_write_marking_codes(self, post_mock):
        response = Mock(status_code=200)
        response.json.return_value = [
            {
                "cisInfo": {
                    "gtin": "04600000000000",
                    "status": "INTRODUCED",
                    "statusEx": "EMPTY",
                }
            }
        ]
        post_mock.return_value = response
        request = self._post_json(
            "/marking/status/check/",
            {"code": "010460000000000021READONLY12345"},
        )
        before = MarkingCode.objects.count()

        api_response = honest_sign_status_check_response(request=request)

        self.assertEqual(api_response.status_code, 200)
        self.assertEqual(MarkingCode.objects.count(), before)

    def test_honest_sign_status_page_uses_processing_head_navigation(self):
        head_user = get_user_model().objects.create_user(
            username="marking_processing_head",
            password="pwd",
        )
        Employee.objects.create(
            user=head_user,
            full_name="Руководитель обработки",
            role="processing_head",
            is_active=True,
        )
        request = self.factory.get("/marking/status/")
        request.user = head_user

        response = honest_sign_status_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/processing-head/"')
        self.assertContains(response, "Руководитель обработки")

    def test_honest_sign_duplicate_validation_preserves_full_scanned_code(self):
        raw_code = "]d2010200000000100021SERIAL123\x1d91ABCD\x1d92CRYPTO"
        request = self._post_json(
            "/marking/duplicate/validate/",
            {"barcode": "2000000001000", "code": raw_code},
        )

        response = honest_sign_duplicate_validate_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["matrix_code"],
            "010200000000100021SERIAL123\x1d91ABCD\x1d92CRYPTO",
        )
        self.assertEqual(payload["gtin"], "02000000001000")
        self.assertEqual(payload["label_template"]["label_key"], "item_cz")
        self.assertEqual(payload["label_template"]["template_key"], "item_cz")
        self.assertEqual(payload["label_template"]["width_mm"], 58.0)
        self.assertEqual(payload["label_template"]["height_mm"], 40.0)
        self.assertTrue(payload["token"])
        self.assertFalse(payload["status_verified"])

    @patch(
        "marking.services.load_processing_param_template_bindings",
        return_value={"item_cz": "item_cz_alt"},
    )
    def test_honest_sign_duplicate_uses_configured_cz_template(self, _bindings_mock):
        request = self._post_json(
            "/marking/duplicate/validate/",
            {
                "barcode": "2000000001000",
                "code": "010460000000000021SERIAL1234567",
            },
        )

        response = honest_sign_duplicate_validate_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["label_template"]["template_key"], "item_cz_alt")

    def test_honest_sign_duplicate_accepts_datamatrix_without_gtin_barcode_match(self):
        request = self._post_json(
            "/marking/duplicate/validate/",
            {
                "barcode": "2000000001000",
                "code": "010460000000000021SERIAL1234567",
            },
        )

        response = honest_sign_duplicate_validate_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["barcode"], "2000000001000")
        self.assertEqual(payload["gtin"], "04600000000000")
        self.assertEqual(payload["matrix_code"], "010460000000000021SERIAL1234567")

    def test_honest_sign_duplicate_does_not_require_sku_honest_sign_enabled(self):
        self.sku.honest_sign = False
        self.sku.save(update_fields=["honest_sign"])
        request = self._post_json(
            "/marking/duplicate/validate/",
            {
                "barcode": "2000000001000",
                "code": "010460000000000021SERIAL1234567",
            },
        )

        response = honest_sign_duplicate_validate_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["sku_code"], "SKU-1")

    def test_honest_sign_duplicate_rejects_swapped_scan_fields(self):
        matrix_code = "010460000000000021SERIAL1234567"
        matrix_in_barcode_request = self._post_json(
            "/marking/duplicate/validate/",
            {"barcode": matrix_code, "code": matrix_code},
        )
        barcode_in_matrix_request = self._post_json(
            "/marking/duplicate/validate/",
            {"barcode": "2000000001000", "code": "2000000001000"},
        )

        matrix_in_barcode_response = honest_sign_duplicate_validate_response(
            request=matrix_in_barcode_request
        )
        barcode_in_matrix_response = honest_sign_duplicate_validate_response(
            request=barcode_in_matrix_request
        )

        self.assertEqual(matrix_in_barcode_response.status_code, 400)
        self.assertFalse(json.loads(matrix_in_barcode_response.content)["ok"])
        self.assertEqual(barcode_in_matrix_response.status_code, 400)
        self.assertFalse(json.loads(barcode_in_matrix_response.content)["ok"])

    @patch("processing_app.services.ProcessingWorkflowService.enqueue_processing_print_job")
    def test_honest_sign_duplicate_print_queues_one_copy_without_marking_write(self, enqueue_mock):
        raw_code = "010460000000000021SERIAL123\x1d91ABCD\x1d92CRYPTO"
        configured_template = {
            "key": "item_cz",
            "title": "Товар ЧЗ",
            "template_title": "Основной",
            "width_mm": 60,
            "height_mm": 50,
        }
        with patch(
            "marking.services.get_effective_label_template",
            return_value=configured_template,
        ):
            validate_request = self._post_json(
                "/marking/duplicate/validate/",
                {"barcode": "2000000001000", "code": raw_code},
            )
            validated = json.loads(
                honest_sign_duplicate_validate_response(request=validate_request).content
            )
            enqueue_mock.return_value = SimpleNamespace(
                http_status=200,
                payload={"ok": True, "job_id": 17, "job_ids": [17], "queued_count": 1},
            )
            png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
            before = MarkingCode.objects.count()
            print_request = self._post_json(
                "/marking/duplicate/print/",
                {
                    "barcode": "2000000001000",
                    "code": raw_code,
                    "token": validated["token"],
                    "label_png_base64": png,
                    "agent_id": "agent-1",
                    "printer_name": "TSC TE200",
                },
            )

            response = honest_sign_duplicate_print_response(request=print_request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(MarkingCode.objects.count(), before)
        call_data = enqueue_mock.call_args.kwargs["data"]
        self.assertEqual(call_data["copies"], 1)
        self.assertEqual(call_data["card_id"], "honest-sign-duplicate")
        self.assertEqual(call_data["template_key"], "item_cz")
        self.assertEqual(call_data["label_width_mm"], 60)
        self.assertEqual(call_data["label_height_mm"], 50)
        audit = AuditEntry.objects.get(journal__code="honest_sign_duplicate")
        self.assertEqual(audit.snapshot["print_job_ids"], [17])
        self.assertNotIn("code", audit.snapshot)

    def test_honest_sign_duplicate_page_uses_processing_head_navigation(self):
        head_user = get_user_model().objects.create_user(
            username="duplicate_processing_head",
            password="pwd",
        )
        Employee.objects.create(
            user=head_user,
            full_name="Руководитель обработки",
            role="processing_head",
            is_active=True,
        )
        request = self.factory.get("/marking/duplicate/")
        request.user = head_user

        response = honest_sign_duplicate_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="/processing-head/"')
        self.assertContains(response, "Дублирование Честного знака")
        self.assertContains(response, 'id="labels-item-renderer"')
        self.assertContains(response, 'type: "fullbox:render-item-label"')
        self.assertContains(response, "if (shouldAutoPrint) queueDuplicatePrint();")
        self.assertContains(response, 'printButton.addEventListener("click", queueDuplicatePrint)')
        self.assertContains(response, "if (printSuccessMessage)")
        self.assertContains(response, "Сканируйте ШК следующего товара")
        self.assertContains(response, "учет ЧЗ, GTIN и привязка КИЗ к товару не проверяются")
        self.assertContains(response, "function isHonestSignScan")
        self.assertContains(response, "В поле ШК товара отсканирован Честный знак")
        self.assertContains(response, "В поле Честного знака отсканирован обычный штрихкод")
        self.assertContains(response, 'const SERIAL_DEVICE_STORAGE_KEY = "fullbox_duplicate_scanner_usb"')
        self.assertContains(response, "function serialPortIdentity")
        self.assertContains(response, "function selectSerialScanner")
        self.assertContains(response, 'const AGENT_EVENTS_URL = "/agent/events/poll/"')
        self.assertContains(response, 'const AGENT_CONTEXT_CLAIM_URL = "/agent/contexts/claim/"')
        self.assertContains(response, "function claimScannerAgentContext")
        self.assertContains(response, "function pollScannerAgentEvents")
        self.assertContains(response, "async function initializePrintAgents")
        self.assertContains(response, "window.fullboxDesktop.getStatus()")
        self.assertContains(response, "desktop_direct: true")
        self.assertContains(response, "Desktop · напрямую")
        self.assertContains(response, "selectedAgent.is_online || selectedAgent.desktop_direct")
        self.assertContains(response, "Печатаю одну копию напрямую через Fullbox Desktop")
        self.assertContains(response, 'serialConnect.textContent = "Сканирование через агент"')
        self.assertContains(response, 'serialConnect.textContent = "Сканирование через Desktop"')
        self.assertNotContains(response, "openSerialScanner(ports[0])")
        self.assertNotContains(response, 'id="label-canvas"')

    def test_honest_sign_duplicate_keeps_offline_agent_and_resolves_host_alias(self):
        DeviceAgent.objects.create(
            agent_id="pc-old-comp002",
            name="COMP002",
            host="COMP002",
            last_seen=timezone.now() - timedelta(days=1),
            meta={"printers": ["TSC TE200 old"]},
        )
        current = DeviceAgent.objects.create(
            agent_id="desktop:desktop-comp002-2",
            name="COMP002",
            host="COMP002",
            last_seen=timezone.now() - timedelta(minutes=5),
            meta={"printers": ["TSC TE200 этикетка 11"]},
        )

        agents = _duplicate_print_agents()

        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0]["id"], current.agent_id)
        self.assertFalse(agents[0]["is_online"])
        self.assertEqual(agents[0]["printers"], ["TSC TE200 этикетка 11"])
        self.assertEqual(agents[0]["aliases"], ["pc-old-comp002"])

    def test_free_marking_import_binds_codes_to_selected_processing_item(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["КИЗ"])
        sheet.append(["010460000000000021FREEPRINT0001"])
        buffer = BytesIO()
        workbook.save(buffer)
        upload = SimpleUploadedFile(
            "free-codes.xlsx",
            buffer.getvalue(),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        request = self.factory.post(
            "/marking/free-print/import/",
            data={
                "order_type": "processing",
                "order_id": "PROC-1",
                "sku_code": "SKU-1",
                "size": "42",
                "barcode": "2000000001000",
                "file": upload,
            },
        )
        request.user = self.head_user

        response = free_marking_import_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["added"], 1)
        code = MarkingCode.objects.get(code="010460000000000021FREEPRINT0001")
        self.assertEqual(code.order_type, "processing")
        self.assertEqual(code.order_id, "PROC-1")
        self.assertEqual(code.agency, self.agency)
        self.assertEqual(code.sku, self.sku)

    def test_free_marking_candidates_only_returns_selected_order_item(self):
        MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINT0002",
            source="import",
            created_by=self.head_user,
        )
        request = self.factory.post(
            "/marking/free-print/candidates/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "qty": 1,
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_candidates_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["codes"], ["010460000000000021FREEPRINT0002"])

    def test_free_marking_queue_reserves_each_code_for_exact_print_job(self):
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINT0003",
            source="import",
            created_by=self.head_user,
        )
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
        request = self.factory.post(
            "/marking/free-print/queue/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "agent_id": "agent-1",
                    "printer_name": "TSC TE200",
                    "labels": [
                        {
                            "code": code.code,
                            "label_png_base64": png,
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_queue_response(request=request)
        payload = json.loads(response.content)
        code.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(code.print_job_id, payload["job_id"])
        self.assertTrue(ProcessingPrintJob.objects.filter(pk=payload["job_id"]).exists())
        self.assertEqual(
            ProcessingPrintJob.objects.get(pk=payload["job_id"]).processing_param_key,
            "free_marking_print",
        )
        self.assertIsNotNone(code.print_reserved_at)
        self.assertIsNone(code.printed_at)
        self.assertEqual(code.printed_by, self.head_user)

    def test_free_marking_queue_reserves_multiple_codes_for_batch_print_job(self):
        codes = [
            MarkingCode.objects.create(
                order_type="processing",
                order_id="PROC-1",
                agency=self.agency,
                sku=self.sku,
                sku_code="SKU-1",
                size="42",
                barcode="2000000001000",
                code=f"010460000000000021FREEPRINTBATCH{index}",
                source="import",
                created_by=self.head_user,
            )
            for index in range(2)
        ]
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
        request = self.factory.post(
            "/marking/free-print/queue/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "agent_id": "agent-1",
                    "printer_name": "TSC TE200",
                    "labels": [
                        {"code": code.code, "label_png_base64": png}
                        for code in codes
                    ],
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_queue_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["queued_count"], 2)
        self.assertEqual(len(payload["job_ids"]), 1)
        batch_job = ProcessingPrintJob.objects.get(pk=payload["job_ids"][0])
        self.assertEqual(batch_job.copies_count, 2)
        self.assertEqual(len(batch_job.label_png_base64_list), 2)
        for code in codes:
            code.refresh_from_db()
            self.assertEqual(code.print_job_id, batch_job.id)
            self.assertIsNotNone(code.print_reserved_at)
            self.assertIsNone(code.printed_at)
            self.assertEqual(code.printed_by, self.head_user)

    def test_free_marking_queue_retry_with_same_request_id_returns_existing_job(self):
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINTIDEMPOTENT",
            source="import",
            created_by=self.head_user,
        )
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nidempotent").decode("ascii")
        payload = {
            "order_type": "processing",
            "order_id": "PROC-1",
            "sku_code": "SKU-1",
            "size": "42",
            "barcode": "2000000001000",
            "request_id": "test-request-1234567890",
            "agent_id": "agent-1",
            "printer_name": "TSC TE200",
            "labels": [{"code": code.code, "label_png_base64": png}],
        }

        first_request = self._post_json("/marking/free-print/queue/", payload)
        first_request.user = self.head_user
        second_request = self._post_json("/marking/free-print/queue/", payload)
        second_request.user = self.head_user
        first = free_marking_queue_response(request=first_request)
        second = free_marking_queue_response(request=second_request)
        first_payload = json.loads(first.content)
        second_payload = json.loads(second.content)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second_payload["job_ids"], first_payload["job_ids"])
        self.assertTrue(second_payload["recovered"])
        self.assertEqual(ProcessingPrintJob.objects.count(), 1)

    def test_free_marking_queue_prints_requested_copies_of_same_kiz(self):
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINTCOPIES",
            source="import",
            created_by=self.head_user,
        )
        png_first = base64.b64encode(b"\x89PNG\r\n\x1a\ncopy-1-of-2").decode("ascii")
        png_second = base64.b64encode(b"\x89PNG\r\n\x1a\ncopy-2-of-2").decode("ascii")
        request = self.factory.post(
            "/marking/free-print/queue/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "agent_id": "agent-1",
                    "printer_name": "TSC TE200",
                    "copies": 2,
                    "labels": [
                        {
                            "code": code.code,
                            "label_png_base64": png_first,
                            "copy_label_png_base64_list": [png_first, png_second],
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        with patch(
            "marking.services._duplicate_label_template",
            return_value={
                "label_key": "item_cz",
                "template_key": "item_cz",
                "width_mm": 60,
                "height_mm": 50,
            },
        ):
            response = free_marking_queue_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["kiz_count"], 1)
        self.assertEqual(payload["copies_per_kiz"], 2)
        self.assertEqual(payload["queued_count"], 2)
        self.assertEqual(len(payload["job_ids"]), 1)
        job = ProcessingPrintJob.objects.get(pk=payload["job_ids"][0])
        self.assertEqual(job.copies_count, 2)
        self.assertEqual(job.label_width_mm, 60)
        self.assertEqual(job.label_height_mm, 50)
        self.assertEqual(job.label_png_base64_list, [png_first, png_second])
        agent_result = ProcessingWorkflowService.processing_print_jobs_next(
            agent_name="agent-1"
        )
        self.assertEqual(agent_result.status, "ok")
        self.assertEqual(
            agent_result.payload["job"]["labelPngBase64List"],
            [png_first, png_second],
        )
        code.refresh_from_db()
        self.assertEqual(code.print_job_id, job.id)
        audit = AuditEntry.objects.filter(journal__code="free_marking_print").latest("id")
        self.assertEqual(audit.snapshot["count"], 1)
        self.assertEqual(audit.snapshot["copies_per_kiz"], 2)
        self.assertEqual(audit.snapshot["print_label_count"], 2)

    def test_free_marking_queue_rejects_more_than_hundred_copies(self):
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINTTOOMANYCOPIES",
            source="import",
            created_by=self.head_user,
        )
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
        request = self.factory.post(
            "/marking/free-print/queue/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "agent_id": "agent-1",
                    "printer_name": "TSC TE200",
                    "copies": 101,
                    "labels": [{"code": code.code, "label_png_base64": png}],
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_queue_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 400)
        self.assertIn("количество копий от 1 до 100", payload["error"])
        code.refresh_from_db()
        self.assertIsNone(code.print_job_id)

    def test_print_agent_completion_marks_bound_kiz_as_printed(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTING,
            barcode="2000000001000",
            printer_name="TSC TE200",
            agent="agent-1",
        )
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINT0004",
            source="import",
            created_by=self.head_user,
            printed_by=self.head_user,
            print_job_id=job.id,
        )

        result = ProcessingWorkflowService.processing_print_jobs_complete(
            data={"job_id": job.id, "status": ProcessingPrintJob.STATUS_PRINTED}
        )
        code.refresh_from_db()

        self.assertEqual(result.http_status, 200)
        self.assertIsNotNone(code.printed_at)
        self.assertEqual(code.print_job_id, job.id)

    def test_print_agent_failure_releases_bound_kiz(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTING,
            barcode="2000000001000",
            printer_name="TSC TE200",
            agent="agent-1",
            processing_param_key="free_marking_print",
        )
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINT0005",
            source="import",
            created_by=self.head_user,
            printed_by=self.head_user,
            print_job_id=job.id,
        )

        result = ProcessingWorkflowService.processing_print_jobs_complete(
            data={"job_id": job.id, "status": ProcessingPrintJob.STATUS_FAILED, "error": "paper"}
        )
        code.refresh_from_db()

        self.assertEqual(result.http_status, 200)
        self.assertIsNone(code.print_job_id)
        self.assertIsNone(code.print_reserved_at)
        self.assertIsNone(code.printed_by)
        self.assertIsNone(code.printed_at)

    def test_free_marking_confirmation_yes_keeps_kiz_printed(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTED,
            order_id="PROC-1",
            barcode="2000000001000",
            processing_param_key="free_marking_print",
            printer_name="TSC TE200",
            agent="agent-1",
        )
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINTYES",
            source="import",
            created_by=self.head_user,
            printed_by=self.head_user,
            printed_at=timezone.now(),
            print_job_id=job.id,
            print_reserved_at=timezone.now(),
        )
        request = self.factory.post(
            "/marking/free-print/confirm/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "job_ids": [job.id],
                    "printed": True,
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_confirm_printed_response(request=request)
        payload = json.loads(response.content)
        code.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["printed"])
        self.assertIsNotNone(code.printed_at)
        self.assertEqual(code.print_job_id, job.id)
        self.assertEqual(payload["stats"]["printed"], 1)
        job.refresh_from_db()
        self.assertEqual(job.processing_param_key, "free_marking_confirmed")

    def test_free_marking_confirmation_no_releases_kiz(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTED,
            order_id="PROC-1",
            barcode="2000000001000",
            processing_param_key="free_marking_print",
            printer_name="TSC TE200",
            agent="agent-1",
        )
        code = MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINTNO",
            source="import",
            created_by=self.head_user,
            printed_by=self.head_user,
            printed_at=timezone.now(),
            print_job_id=job.id,
            print_reserved_at=timezone.now(),
        )
        request = self.factory.post(
            "/marking/free-print/confirm/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "job_ids": [job.id],
                    "printed": False,
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_confirm_printed_response(request=request)
        payload = json.loads(response.content)
        code.refresh_from_db()
        job.refresh_from_db()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["printed"])
        self.assertIsNone(code.printed_at)
        self.assertIsNone(code.printed_by)
        self.assertIsNone(code.print_job_id)
        self.assertIsNone(code.print_reserved_at)
        self.assertEqual(job.status, ProcessingPrintJob.STATUS_FAILED)
        self.assertEqual(payload["stats"]["available"], 1)

    def test_free_marking_status_only_accepts_linked_batch(self):
        job = ProcessingPrintJob.objects.create(
            status=ProcessingPrintJob.STATUS_PRINTED,
            order_id="PROC-1",
            barcode="2000000001000",
            processing_param_key="free_marking_print",
        )
        MarkingCode.objects.create(
            order_type="processing",
            order_id="PROC-1",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021FREEPRINTSTATUS",
            source="import",
            created_by=self.head_user,
            print_job_id=job.id,
        )
        request = self.factory.post(
            "/marking/free-print/status/",
            data=json.dumps(
                {
                    "order_type": "processing",
                    "order_id": "PROC-1",
                    "sku_code": "SKU-1",
                    "size": "42",
                    "barcode": "2000000001000",
                    "job_ids": [job.id],
                }
            ),
            content_type="application/json",
        )
        request.user = self.head_user

        response = free_marking_batch_status_response(request=request)
        payload = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["terminal"])
        self.assertEqual(payload["failed"], 0)

    def test_free_marking_page_is_available_to_processing_head(self):
        request = self.factory.get(
            "/marking/free-print/?order_type=processing&order_id=PROC-1"
        )
        request.user = self.head_user

        response = free_marking_print_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Свободная печать КИЗ")
        self.assertContains(response, "SKU-1")
        self.assertContains(response, "Все этикетки напечатались?")
        self.assertContains(response, "Да, все напечатались")
        self.assertContains(response, "Количество КИЗ")
        self.assertContains(response, "Копий каждого")
        self.assertContains(response, "две одинаковые этикетки одного ЧЗ")
        self.assertContains(response, 'id="print-qty" type="number" min="1" max="100"')
        self.assertContains(response, 'id="print-copies" type="number" min="1" max="100"')
        self.assertContains(response, "const freePrintReservationChunkSize = 1;")
        self.assertContains(response, "while(queuedKizCount<qty&&!queueError)")
        self.assertContains(response, "queuedCopyCount+=queuedNow")
        self.assertContains(response, "qty*copies>freePrintMaxLabels")
        self.assertContains(response, "copy_label: copyCount > 1")
        self.assertContains(response, "copy_label_png_base64_list:copyImages")
        self.assertContains(response, "const width = Number(template.width_mm) || 58")
        self.assertContains(response, "const height = Number(template.height_mm) || 40")
        self.assertContains(response, "qty:chunkQty")
        self.assertContains(response, "{retryable:true}")
        self.assertContains(response, "if(!jobIds.length)throw queueError||")
        self.assertContains(response, "Сервер временно недоступен и не подтвердил создание задания печати")

    def test_free_marking_batch_accepts_up_to_hundred_print_jobs(self):
        self.assertEqual(
            _free_print_job_ids({"job_ids": list(range(1, 121))}),
            list(range(1, 101)),
        )

    def test_free_marking_page_resolves_external_receiving_number_and_lists_all_items(self):
        second_sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-2",
            name="Брюки",
            size="48",
            honest_sign=True,
        )
        SKUBarcode.objects.create(sku=second_sku, value="2000000002000", size="48")
        OrderAuditEntry.objects.create(
            order_id="PR-000226",
            order_type="receiving",
            action="update",
            agency=self.agency,
            payload={
                "status": "sent_unconfirmed",
                "items": [
                    {
                        "sku_code": "SKU-1",
                        "size": "42",
                        "barcode": "2000000001000",
                        "qty": 2,
                    },
                    {
                        "sku_code": "SKU-2",
                        "size": "48",
                        "barcode": "2000000002000",
                        "qty": 3,
                    },
                ],
            },
        )
        OrderAuditEntry.objects.create(
            order_id="PR-000226",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={"status": "warehouse", "status_label": "В ожидании поставки товара"},
        )
        for index in range(2):
            MarkingCode.objects.create(
                order_type="receiving",
                order_id="PR-000226",
                agency=self.agency,
                sku=self.sku,
                sku_code="SKU-1",
                size="42",
                barcode="2000000001000",
                code=f"010460000000000021RECEIVING{index}",
                source="import",
                created_by=self.head_user,
            )
        request = self.factory.get(
            "/marking/free-print/?order_type=receiving&order_id=226_PR"
        )
        request.user = self.head_user

        response = free_marking_print_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Приемка №226_PR")
        self.assertContains(response, "Товары заявки")
        self.assertContains(response, "Куртка")
        self.assertContains(response, "Брюки")
        self.assertContains(response, "ЧЗ есть")
        self.assertContains(response, "ЧЗ нет")
        self.assertContains(response, "В базе: 2 из 2")
        self.assertContains(response, "В базе: 0 из 3")
        self.assertContains(response, 'order_id: "PR\\u002D000226"')

    def test_free_marking_page_shows_client_free_codes_for_receiving_item(self):
        MarkingCode.objects.create(
            order_type="processing",
            order_id="",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="42",
            barcode="2000000001000",
            code="010460000000000021CLIENTFREE",
            source="import",
            created_by=self.head_user,
        )
        request = self.factory.get(
            "/marking/free-print/?order_type=receiving&order_id=REC-1"
        )
        request.user = self.head_user

        response = free_marking_print_page(request)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ЧЗ частично")
        self.assertContains(response, "В базе: 1 из 2")
        self.assertContains(response, "Свободно у клиента: 1")
        self.assertContains(response, '"client_free": 1')

    def test_free_marking_receiving_size_print_is_recorded_as_printed(self):
        SKUBarcode.objects.create(
            sku=self.sku,
            value="2000000001001",
            size="44",
        )
        OrderAuditEntry.objects.create(
            order_id="REC-SIZES",
            order_type="receiving",
            action="status",
            agency=self.agency,
            payload={
                "receiving_mode": "cz",
                "items": [
                    {
                        "sku_code": "SKU-1",
                        "size": "42",
                        "barcode": "2000000001000",
                        "qty": 2,
                    },
                    {
                        "sku_code": "SKU-1",
                        "size": "44",
                        "barcode": "2000000001001",
                        "qty": 3,
                    },
                ],
            },
        )
        code = MarkingCode.objects.create(
            order_type="receiving",
            order_id="REC-SIZES",
            agency=self.agency,
            sku=self.sku,
            sku_code="SKU-1",
            size="44",
            barcode="2000000001001",
            code="010460000000000021FREEPRINTREC44",
            source="import",
            created_by=self.head_user,
        )
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode("ascii")
        queue_request = self.factory.post(
            "/marking/free-print/queue/",
            data=json.dumps(
                {
                    "order_type": "receiving",
                    "order_id": "REC-SIZES",
                    "sku_code": "SKU-1",
                    "size": "44",
                    "barcode": "2000000001001",
                    "agent_id": "agent-1",
                    "printer_name": "TSC TE200",
                    "labels": [
                        {
                            "code": code.code,
                            "label_png_base64": png,
                        }
                    ],
                }
            ),
            content_type="application/json",
        )
        queue_request.user = self.head_user

        queue_response = free_marking_queue_response(request=queue_request)
        queue_payload = json.loads(queue_response.content)

        self.assertEqual(queue_response.status_code, 200)
        self.assertEqual(queue_payload["stats"]["queued"], 1)
        self.assertEqual(queue_payload["stats"]["printed"], 0)

        complete_result = ProcessingWorkflowService.processing_print_jobs_complete(
            data={
                "job_id": queue_payload["job_id"],
                "status": ProcessingPrintJob.STATUS_PRINTED,
            }
        )
        self.assertEqual(complete_result.http_status, 200)

        confirm_request = self.factory.post(
            "/marking/free-print/confirm/",
            data=json.dumps(
                {
                    "order_type": "receiving",
                    "order_id": "REC-SIZES",
                    "sku_code": "SKU-1",
                    "size": "44",
                    "barcode": "2000000001001",
                    "job_ids": [queue_payload["job_id"]],
                    "printed": True,
                }
            ),
            content_type="application/json",
        )
        confirm_request.user = self.head_user
        confirm_response = free_marking_confirm_printed_response(request=confirm_request)
        self.assertEqual(confirm_response.status_code, 200)

        page_request = self.factory.get(
            "/marking/free-print/?order_type=receiving&order_id=REC-SIZES"
        )
        page_request.user = self.head_user
        page_response = free_marking_print_page(page_request)

        self.assertEqual(page_response.status_code, 200)
        self.assertContains(page_response, "Приемка №REC-SIZES")
        self.assertContains(page_response, "SKU-1 · размер 42")
        self.assertContains(page_response, "SKU-1 · размер 44")
        self.assertContains(page_response, '"queued": 0')
        self.assertContains(page_response, '"printed": 1')
