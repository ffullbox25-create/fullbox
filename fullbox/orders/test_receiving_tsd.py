import json
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from agent.models import DeviceAgent
from audit.models import AuditEntry, OrderAuditEntry
from employees.models import Employee
from sku.models import Agency, SKU, SKUBarcode
from sklad.models import WarehouseLocation, WarehouseStockSnapshot
from todo.models import Task

from orders.services import ReceivingWorkflowService
from orders.receiving_tsd import _active_desktop_printer, _agent_rows, _putaway_preview


class ReceivingTsdTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="receiving_tsd_one", password="pwd")
        self.other_user = user_model.objects.create_user(username="receiving_tsd_two", password="pwd")
        self.employee = Employee.objects.create(
            full_name="Кладовщик TSD",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.other_employee = Employee.objects.create(
            full_name="Второй кладовщик TSD",
            role="storekeeper",
            user=self.other_user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент TSD", pref="TST")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="SKU-TSD-1",
            name="Товар для TSD",
            size="0",
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value="200000000001",
            size="0",
            is_primary=True,
        )
        self.order_id = "PR-TSD-1"
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Статус приемки",
            payload={
                "status": "warehouse",
                "status_label": "В ожидании поставки товара",
                "goods_type": "gv",
                "receiving_mode": "standard",
                "items": [
                    {
                        "sku_code": self.sku.sku_code,
                        "name": self.sku.name,
                        "size": "0",
                        "barcode": "200000000001",
                        "qty": 2,
                    }
                ],
            },
        )
        Task.objects.create(
            title=f"Принять заявку на приемку товара №{self.order_id}",
            description="Приемка",
            route=f"/orders/receiving/{self.order_id}/",
            assigned_to=self.employee,
            created_by=self.user,
        )
        self.client.force_login(self.user)

    def _claim_and_close_one_pallet(self):
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        opened = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/open/",
            data=json.dumps({"event_id": "open-for-pallet"}),
            content_type="application/json",
        )
        self.assertEqual(opened.status_code, 200, opened.content)
        scanned = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/scan/",
            data=json.dumps({"barcode": "200000000001", "event_id": "scan-for-pallet"}),
            content_type="application/json",
        )
        self.assertEqual(scanned.status_code, 200, scanned.content)
        closed_box = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/close/",
            data=json.dumps({"event_id": "close-box-for-pallet"}),
            content_type="application/json",
        )
        self.assertEqual(closed_box.status_code, 200, closed_box.content)
        with patch(
            "orders.receiving_tsd._putaway_preview",
            return_value=[
                {
                    "pallet_code": "PALLET",
                    "destination_code": "OS-A1",
                    "destination_label": "OS · Ячейка A1",
                }
            ],
        ):
            closed_pallet = self.client.post(
                f"/orders/receiving/{self.order_id}/tsd/pallet/close/",
                data=json.dumps({"event_id": "close-pallet"}),
                content_type="application/json",
            )
        self.assertEqual(closed_pallet.status_code, 200, closed_pallet.content)
        return closed_pallet

    def _assign_receiving_owner(self, employee):
        current = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="receiving")
            .order_by("-created_at", "-id")
            .first()
        )
        payload = dict(current.payload or {})
        payload.update(
            {
                "status": "warehouse",
                "status_label": "Взята в работу",
                "storekeeper_employee_id": employee.id,
                "storekeeper_name": employee.full_name,
            }
        )
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=employee.user,
            description="Заявка взята в работу кладовщиком",
            payload=payload,
        )
        Task.objects.filter(route=f"/orders/receiving/{self.order_id}/").update(
            assigned_to=employee,
            status="in_progress",
        )

    def _set_chz_service(self, service):
        status_entry = (
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="receiving",
                action="status",
            )
            .order_by("-created_at", "-id")
            .first()
        )
        payload = dict(status_entry.payload or {})
        payload["receiving_chz_service"] = service
        status_entry.payload = payload
        status_entry.save(update_fields=["payload"])

    def test_print_agents_use_current_desktop_printers_and_preferred_queue(self):
        DeviceAgent.objects.create(
            agent_id="legacy-comp008",
            name="COMP008",
            host="COMP008",
            meta={"printers": ["Xprinter XP-365B"]},
        )
        with patch(
            "orders.receiving_tsd.get_desktop_presences",
            return_value={
                "desktop-comp008": {
                    "last_seen": "2026-09-13T00:25:00+03:00",
                    "printers": [
                        "Xprinter XP-365B",
                        "Xprinter XP-365B (копия 3)",
                    ],
                    "printer_details": [
                        {
                            "name": "Xprinter XP-365B",
                            "is_busy": True,
                            "jobs": 37,
                        },
                        {
                            "name": "Xprinter XP-365B (копия 3)",
                            "is_default": True,
                            "jobs": 0,
                        },
                    ],
                    "preferred_printer": "Xprinter XP-365B (копия 3)",
                }
            },
        ):
            rows = _agent_rows()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["agent_id"], "desktop:desktop-comp008")
        self.assertEqual(
            rows[0]["printers"],
            ["Xprinter XP-365B", "Xprinter XP-365B (копия 3)"],
        )
        self.assertEqual(
            rows[0]["preferred_printer"], "Xprinter XP-365B (копия 3)"
        )
        self.assertEqual(rows[0]["active_printer"], "Xprinter XP-365B (копия 3)")
        self.assertTrue(rows[0]["printer_ready"])

    def test_active_desktop_printer_rejects_unhealthy_queues(self):
        self.assertEqual(
            _active_desktop_printer(
                ["Xprinter XP-365B", "Xprinter XP-365B (копия 1)"],
                {
                    "preferred_printer": "Xprinter XP-365B",
                    "printer_details": [
                        {"name": "Xprinter XP-365B", "jobs": 37},
                        {
                            "name": "Xprinter XP-365B (копия 1)",
                            "is_offline": True,
                            "jobs": 0,
                        },
                    ],
                },
            ),
            "",
        )

    def test_queue_lists_receiving_task(self):
        response = self.client.get("/orders/receiving/tsd/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order_id)
        self.assertContains(response, "Клиент TSD")
        self.assertContains(response, "Взять в работу")

    def test_queue_has_receiving_logout_button(self):
        response = self.client.get("/orders/receiving/tsd/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            '<a class="tsd-exit-button" href="/tsd/receiving/?switch=1">Выйти</a>',
            html=True,
        )

    def test_tsd_asset_is_served_to_storekeeper(self):
        response = self.client.get(
            reverse("orders-receiving-tsd-asset", args=["receiving_tsd.css"])
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/css"))

    def test_claim_locks_order_before_first_item_scan(self):
        response = self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")

        self.assertEqual(response.status_code, 302)
        entries = list(
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="receiving")
            .order_by("created_at", "id")
        )
        owner = ReceivingWorkflowService._receiving_work_owner(entries)
        self.assertEqual(owner.get("storekeeper_employee_id"), self.employee.id)
        self.assertTrue(entries[-1].payload.get("receiving_tsd_claimed"))

        second = ReceivingWorkflowService.start_receiving_work(
            order_id=self.order_id,
            entries=entries,
            role="storekeeper",
            user=self.other_user,
        )
        self.assertEqual(second.status, "assigned_to_other_storekeeper")

    def test_existing_owner_can_open_before_first_item_scan(self):
        self._assign_receiving_owner(self.employee)

        claim = self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        detail = self.client.get(claim["Location"])

        self.assertEqual(claim.status_code, 302)
        self.assertEqual(detail.status_code, 200)
        self.assertTemplateUsed(detail, "orders/receiving_tsd_detail.html")

    def test_other_storekeeper_can_confirm_takeover(self):
        self._assign_receiving_owner(self.other_employee)

        queue = self.client.get("/orders/receiving/tsd/")
        self.assertContains(queue, self.other_employee.full_name)
        self.assertContains(queue, 'name="take_over"')
        self.assertContains(queue, 'value="1"')

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/claim/",
            {"take_over": "1"},
        )

        self.assertEqual(response.status_code, 302)
        entries = list(
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="receiving")
            .order_by("created_at", "id")
        )
        owner = ReceivingWorkflowService._receiving_work_owner(entries)
        self.assertEqual(owner.get("storekeeper_employee_id"), self.employee.id)

    def test_queue_hides_closed_receiving_task(self):
        current = (
            OrderAuditEntry.objects.filter(order_id=self.order_id, order_type="receiving")
            .order_by("-created_at", "-id")
            .first()
        )
        payload = dict(current.payload or {})
        payload.update({"status": "done", "status_label": "Выполнена", "flow_closed": True})
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Приемка завершена",
            payload=payload,
        )

        response = self.client.get("/orders/receiving/tsd/")

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.order_id)

    def test_open_box_and_scan_are_saved_server_side_and_idempotent(self):
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        open_response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/open/",
            data=json.dumps({"event_id": "open-1"}),
            content_type="application/json",
        )
        self.assertEqual(open_response.status_code, 200, open_response.content)
        self.assertTrue(open_response.json().get("summary", {}).get("active_box"))

        scan_payload = {"barcode": "200000000001", "event_id": "scan-1"}
        first_scan = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/scan/",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(first_scan.status_code, 200, first_scan.content)
        self.assertEqual(first_scan.json()["summary"]["accepted_qty"], 1)

        repeated_scan = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/scan/",
            data=json.dumps(scan_payload),
            content_type="application/json",
        )
        self.assertEqual(repeated_scan.status_code, 200, repeated_scan.content)
        self.assertTrue(repeated_scan.json().get("duplicate_ignored"))
        self.assertEqual(repeated_scan.json()["summary"]["accepted_qty"], 1)
        self.assertEqual(
            AuditEntry.objects.filter(
                journal__code="receiving_tsd",
                snapshot__event_type="scan_ok",
                snapshot__order_id=self.order_id,
            ).count(),
            1,
        )

    def test_required_chz_service_is_effective_without_saved_mode(self):
        status_entry = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            order_type="receiving",
            action="status",
        ).first()
        status_entry.payload.pop("receiving_mode", None)
        status_entry.payload["receiving_chz_service"] = "required"
        status_entry.save(update_fields=["payload"])

        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        response = self.client.get(f"/orders/receiving/{self.order_id}/tsd/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["receiving_mode"], "cz")
        self.assertContains(response, "Честный знак")
        self.assertContains(response, 'id="marking-mode-toggle"', html=False)
        self.assertContains(response, "Клиент указал обязательное сканирование")

    def test_storekeeper_can_enable_optional_chz_before_first_scan(self):
        self._set_chz_service("not_required")
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")

        page = self.client.get(f"/orders/receiving/{self.order_id}/tsd/")
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context["is_cz"])
        self.assertTrue(page.context["marking_mode_can_change"])
        self.assertContains(page, "При необходимости включите до первого скана товара")

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/marking-mode/",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["is_cz"])
        latest = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            order_type="receiving",
        ).latest("id")
        self.assertEqual((latest.payload or {}).get("receiving_mode"), "cz")

        scan_page = self.client.get(f"/orders/receiving/{self.order_id}/tsd/")
        self.assertTrue(scan_page.context["is_cz"])
        self.assertContains(scan_page, "ШК товара, затем Data Matrix")

    def test_storekeeper_cannot_change_chz_before_claiming_order(self):
        self._set_chz_service("not_required")

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/marking-mode/",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("возьмите заявку в работу", response.json()["error"].lower())

    def test_required_chz_cannot_be_disabled_by_storekeeper(self):
        self._set_chz_service("required")
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/marking-mode/",
            data=json.dumps({"enabled": False}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("клиент выбрал", response.json()["error"].lower())
        latest = OrderAuditEntry.objects.filter(
            order_id=self.order_id,
            order_type="receiving",
        ).latest("id")
        self.assertEqual(ReceivingWorkflowService.effective_receiving_mode(latest.payload), "cz")

    def test_marking_mode_is_locked_after_first_accepted_item(self):
        self._set_chz_service("not_required")
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        opened = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/open/",
            data=json.dumps({"event_id": "open-before-mode-lock"}),
            content_type="application/json",
        )
        self.assertEqual(opened.status_code, 200, opened.content)
        scanned = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/scan/",
            data=json.dumps({"barcode": "200000000001", "event_id": "scan-before-mode-lock"}),
            content_type="application/json",
        )
        self.assertEqual(scanned.status_code, 200, scanned.content)

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/marking-mode/",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn("после первого принятого товара", response.json()["error"])

    def test_optional_chz_requires_data_matrix_after_enabled(self):
        self._set_chz_service("not_required")
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        mode = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/marking-mode/",
            data=json.dumps({"enabled": True}),
            content_type="application/json",
        )
        self.assertEqual(mode.status_code, 200, mode.content)
        opened = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/open/",
            data=json.dumps({"event_id": "open-optional-chz"}),
            content_type="application/json",
        )
        self.assertEqual(opened.status_code, 200, opened.content)

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/scan/",
            data=json.dumps({"barcode": "200000000001", "event_id": "scan-optional-chz"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["requires_marking"])

    def test_explicit_chz_service_overrides_stale_standard_mode(self):
        self.assertEqual(
            ReceivingWorkflowService.effective_receiving_mode(
                {
                    "goods_type": "gv",
                    "receiving_mode": "standard",
                    "receiving_chz_service": "required",
                }
            ),
            "cz",
        )

    def test_storekeeper_can_close_pallet_and_see_destination_preview(self):
        response = self._claim_and_close_one_pallet()

        self.assertEqual(response.json()["summary"]["sealed_pallet_count"], 1)
        self.assertFalse(response.json()["summary"]["active_pallet"])
        self.assertEqual(
            response.json()["putaway_preview"][0]["destination_code"],
            "OS-A1",
        )

    def test_storekeeper_must_scan_concrete_pr_location(self):
        self.client.post(f"/orders/receiving/{self.order_id}/tsd/claim/")
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=1,
            location_code="PR-TEST-01",
            display_name="PR · Стол 1",
            is_active=True,
        )

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/location/",
            data=json.dumps({"location_code": "PR-TEST-01", "event_id": "location-1"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["receiving_location"]["code"], "PR-TEST-01")
        self.assertTrue(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__event="receiving_tsd_location_selected",
                payload__receiving_location_code="PR-TEST-01",
            ).exists()
        )

    def test_storekeeper_cannot_scan_fbs_pr_location(self):
        self._assign_receiving_owner(self.employee)
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            location_code="P-3-1/2-1",
            display_name="Зона PR",
            is_fbs_visible=True,
            is_active=True,
        )

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/location/",
            data=json.dumps({"location_code": "P-3-1/2-1", "event_id": "location-fbs-1"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertTrue(
            response.json()["error"].startswith("Место P-3-1/2-1 относится к FBS."),
            response.json()["error"],
        )
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__event="receiving_tsd_location_selected",
            ).exists()
        )

    def test_general_pr_location_remains_actual_pallet_location(self):
        self._assign_receiving_owner(self.employee)
        closed_pallet = self._claim_and_close_one_pallet()
        pallet_code = closed_pallet.json()["pallet_code"]
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=8,
            location_code="PR-TEST-08",
            display_name="PR · Стол 8",
            is_fbs_visible=False,
            is_active=True,
        )

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/location/",
            data=json.dumps(
                {
                    "location_code": "PR-TEST-08",
                    "pallet_code": pallet_code,
                    "event_id": "location-actual-general-pr",
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        preview = next(
            row
            for row in response.json()["putaway_preview"]
            if row["pallet_code"] == pallet_code
        )
        self.assertEqual(preview["current_location_label"], "PR · Стол 8")
        self.assertFalse(preview["needs_pr_location"])
        self.assertEqual(
            ReceivingWorkflowService.build_receiving_flow_actual_pallet_locations(
                self.order_id,
                [{"code": pallet_code}],
            )[pallet_code],
            "PR · Стол 8",
        )

    def test_fbs_route_putaway_preview_keeps_exact_fbs_destination(self):
        context = SimpleNamespace(
            order_id=self.order_id,
            agency=self.agency,
            entries=[SimpleNamespace(payload={"receiving_route": "fbs"})],
        )
        state = {
            "boxes": [],
            "pallets": [
                {
                    "code": "PAL-FBS-1",
                    "sealed": True,
                    "items": [{"qty": 1}],
                }
            ],
        }

        with patch(
            "fbs.services.receiving_movements.receiving_fbs_route_rows",
            return_value=[
                {
                    "source_pallet_code": "PAL-FBS-1",
                    "cell_code": "P-3-1/2-1",
                    "cell_label": "Линия P · этаж 2 · ячейка 1",
                    "pallet_code": "FBS-PAL-1",
                }
            ],
        ):
            preview = _putaway_preview(context, state)

        self.assertEqual(preview[0]["destination_code"], "P-3-1/2-1")
        self.assertIn("FBS", preview[0]["destination_label"])
        self.assertIn("FBS-PAL-1", preview[0]["destination_label"])

    def test_scanning_pr_location_makes_latest_closed_pallet_ready_for_driver(self):
        self._assign_receiving_owner(self.employee)
        closed_pallet = self._claim_and_close_one_pallet()
        pallet_code = closed_pallet.json()["pallet_code"]
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=3,
            location_code="PR-TEST-03",
            display_name="PR · Стол 3",
            is_active=True,
        )

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/location/",
            data=json.dumps({"location_code": "PR-TEST-03", "event_id": "location-ready-1"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["placement_pallet_code"], pallet_code)
        self.assertEqual(response.json()["placement_status"]["status"], "ready")
        self.assertTrue(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__event=ReceivingWorkflowService.PALLET_PLACEMENT_PERMISSION_EVENT,
                payload__pallet_code=pallet_code,
                payload__receiving_location_code="PR-TEST-03",
            ).exists()
        )
        snapshots = WarehouseStockSnapshot.objects.filter(
            source_context_type="receiving",
            source_context_id=self.order_id,
            parent_container__container_code=pallet_code,
            is_archived=False,
            qty__gt=0,
        )
        self.assertTrue(snapshots.exists())
        self.assertFalse(snapshots.exclude(location__location_code="PR-TEST-03").exists())
        self.assertTrue(
            ReceivingWorkflowService.receiving_flow_pallet_takeout_state(
                order_id=self.order_id,
                pallet_code=pallet_code,
            )["can_take"]
        )

    def test_completion_creates_reachtruck_task_for_closed_pallet(self):
        closed_pallet = self._claim_and_close_one_pallet()
        pallet_code = closed_pallet.json()["pallet_code"]
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=2,
            location_code="PR-TEST-02",
            display_name="PR · Стол 2",
            is_active=True,
        )
        saved_location = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/location/",
            data=json.dumps({"location_code": "PR-TEST-02", "event_id": "location-2"}),
            content_type="application/json",
        )
        self.assertEqual(saved_location.status_code, 200, saved_location.content)
        preparation = SimpleNamespace(
            status="ok",
            reason="",
            status_payload={"goods_type": "gv"},
            has_mismatch=False,
            receiving_mode="standard",
            act_items=[],
            placement_items=[],
            boxes=[],
            pallets=[],
            flow_state={},
            act_units=[],
            normalized_eta="",
            vehicle_number="",
            has_closed_placement_act=False,
        )
        move_result = SimpleNamespace(
            created_count=1,
            skipped_existing_count=0,
            skipped_missing_destination_count=0,
        )
        with (
            patch.object(
                ReceivingWorkflowService,
                "prepare_receiving_flow_completion",
                return_value=preparation,
            ),
            patch.object(ReceivingWorkflowService, "complete_receiving_flow"),
            patch.object(
                ReceivingWorkflowService,
                "collect_receiving_warehouse_move_destinations",
                return_value={pallet_code: {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1}},
            ),
            patch.object(
                ReceivingWorkflowService,
                "create_receiving_warehouse_moves",
                return_value=move_result,
            ) as create_moves,
        ):
            response = self.client.post(
                f"/orders/receiving/{self.order_id}/tsd/complete/",
                data=json.dumps({"event_id": "complete-1"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["created_count"], 1)
        create_moves.assert_called_once()

    def test_completion_requires_pr_location_for_every_closed_pallet(self):
        self._assign_receiving_owner(self.employee)
        first = self._claim_and_close_one_pallet()
        first_pallet_code = first.json()["pallet_code"]
        WarehouseLocation.objects.create(
            warehouse_code="MSK",
            zone_code="PR",
            zone_kind=WarehouseLocation.ZONE_KIND_RECEIVING,
            row_no=1,
            section_no=1,
            tier_no=1,
            cell_no=7,
            location_code="PR-TEST-07",
            display_name="PR · Стол 7",
            is_active=True,
        )
        saved_location = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/location/",
            data=json.dumps(
                {
                    "location_code": "PR-TEST-07",
                    "pallet_code": first_pallet_code,
                    "event_id": "location-first-pallet",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(saved_location.status_code, 200, saved_location.content)
        first_preview = next(
            row
            for row in saved_location.json()["putaway_preview"]
            if row["pallet_code"] == first_pallet_code
        )
        self.assertEqual(first_preview["current_location_label"], "PR · Стол 7")
        self.assertFalse(first_preview["needs_pr_location"])

        opened = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/open/",
            data=json.dumps({"event_id": "open-second-pallet"}),
            content_type="application/json",
        )
        self.assertEqual(opened.status_code, 200, opened.content)
        scanned = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/scan/",
            data=json.dumps({"barcode": "200000000001", "event_id": "scan-second-pallet"}),
            content_type="application/json",
        )
        self.assertEqual(scanned.status_code, 200, scanned.content)
        closed_box = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/box/close/",
            data=json.dumps({"event_id": "close-second-box"}),
            content_type="application/json",
        )
        self.assertEqual(closed_box.status_code, 200, closed_box.content)
        second = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/pallet/close/",
            data=json.dumps({"event_id": "close-second-pallet"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200, second.content)
        second_pallet_code = second.json()["pallet_code"]

        response = self.client.post(
            f"/orders/receiving/{self.order_id}/tsd/complete/",
            data=json.dumps({"event_id": "complete-with-missing-location"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["status"], "pallet_location_required")
        self.assertIn(second_pallet_code, response.json()["error"])
        self.assertFalse(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                payload__flow_closed=True,
            ).exists()
        )
