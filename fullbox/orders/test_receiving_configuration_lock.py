import json
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase

from audit.models import OrderAuditEntry
from employees.models import Employee
from orders.services import ReceivingWorkflowService
from sku.models import Agency, SKU, SKUBarcode


class ReceivingConfigurationLockTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="receiving_configuration_lock",
            password="pwd",
        )
        self.storekeeper = Employee.objects.create(
            full_name="Кладовщик фиксации приемки",
            role="storekeeper",
            user=self.user,
            is_active=True,
        )
        self.agency = Agency.objects.create(agn_name="Клиент фиксации приемки")
        self.sku = SKU.objects.create(
            agency=self.agency,
            sku_code="LOCK-SKU",
            name="Товар с фиксированным типом",
            size="0",
        )
        SKUBarcode.objects.create(
            sku=self.sku,
            value="200000009901",
            size="0",
            is_primary=True,
        )
        self.order_id = "R-CONFIG-LOCK-1"
        OrderAuditEntry.objects.create(
            order_id=self.order_id,
            order_type="receiving",
            action="status",
            agency=self.agency,
            user=self.user,
            description="Заявка взята в работу кладовщиком",
            payload={
                "status": "warehouse",
                "status_label": "Взята в работу",
                "storekeeper_employee_id": self.storekeeper.id,
                "storekeeper_name": self.storekeeper.full_name,
                "items": [
                    {
                        "sku_code": "LOCK-SKU",
                        "name": "Товар с фиксированным типом",
                        "size": "0",
                        "qty": 1,
                    }
                ],
            },
        )

    def entries(self):
        return list(
            OrderAuditEntry.objects.filter(
                order_id=self.order_id,
                order_type="receiving",
            ).order_by("created_at", "id")
        )

    def configure_initial_type(self):
        return ReceivingWorkflowService.configure_receiving_act(
            order_id=self.order_id,
            entries=self.entries(),
            role="storekeeper",
            goods_type="gv",
            receiving_mode="standard",
            user=self.user,
        )

    def test_current_status_ignores_technical_placement_snapshot(self):
        real_status = SimpleNamespace(
            action="status",
            payload={"status": "warehouse", "status_label": "Взята в работу", "goods_type": "no"},
        )
        placement_snapshot = SimpleNamespace(
            action="status",
            payload={"act": "placement", "act_state": "open"},
        )

        current = ReceivingWorkflowService._current_status_entry(
            [real_status, placement_snapshot]
        )

        self.assertIs(current, real_status)

    def test_first_configuration_is_locked_and_cannot_be_changed(self):
        initial = self.configure_initial_type()

        self.assertTrue(initial.applied)
        self.assertTrue(initial.payload.get("receiving_configuration_locked"))
        self.assertEqual(
            initial.payload.get("receiving_configuration_locked_goods_type"),
            "gv",
        )
        self.assertEqual(
            initial.payload.get("receiving_configuration_locked_mode"),
            "standard",
        )

        changed = ReceivingWorkflowService.configure_receiving_act(
            order_id=self.order_id,
            entries=self.entries(),
            role="storekeeper",
            goods_type="no",
            receiving_mode="standard",
            user=self.user,
        )

        self.assertFalse(changed.applied)
        self.assertIn("зафиксирован", changed.description)
        current = ReceivingWorkflowService._current_status_entry(self.entries())
        self.assertEqual((current.payload or {}).get("goods_type"), "gv")

    def test_marking_mode_cannot_change_after_initial_configuration(self):
        initial = self.configure_initial_type()
        self.assertTrue(initial.applied)

        changed = ReceivingWorkflowService.set_receiving_marking_mode(
            order_id=self.order_id,
            role="storekeeper",
            receiving_mode="cz",
            user=self.user,
        )

        self.assertFalse(changed.applied)
        self.assertIn("зафиксирован", changed.description)

    def test_draft_rejects_new_container_with_another_type(self):
        initial = self.configure_initial_type()
        self.assertTrue(initial.applied)
        box = {
            "code": "LOCK-BOX-1",
            "sealed": False,
            "goods_type": "br",
            "items": [
                {
                    "sku_code": "LOCK-SKU",
                    "name": "Товар с фиксированным типом",
                    "size": "0",
                    "goods_type": "br",
                    "qty": 1,
                }
            ],
        }
        pallet = {
            "code": "LOCK-PALLET-1",
            "sealed": False,
            "goods_type": "br",
            "boxes": [box["code"]],
            "items": [],
            "location": {"zone": "PR"},
        }

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=self.order_id,
            entries=self.entries(),
            role="storekeeper",
            boxes_raw=json.dumps([box]),
            pallets_raw=json.dumps([pallet]),
            active_box=box["code"],
            active_pallet=pallet["code"],
            user=self.user,
        )

        self.assertEqual(result.status, "goods_type_locked")
        self.assertEqual(result.meta.get("container_codes"), [box["code"], pallet["code"]])

    def test_draft_inherits_locked_type_when_legacy_box_type_is_blank(self):
        initial = self.configure_initial_type()
        self.assertTrue(initial.applied)
        box = {
            "code": "LOCK-BOX-2",
            "sealed": False,
            "goods_type": "",
            "items": [
                {
                    "sku_code": "LOCK-SKU",
                    "name": "Товар с фиксированным типом",
                    "size": "0",
                    "goods_type": "",
                    "qty": 1,
                }
            ],
        }
        pallet = {
            "code": "LOCK-PALLET-2",
            "sealed": False,
            "goods_type": "",
            "boxes": [box["code"]],
            "items": [],
            "location": {"zone": "PR"},
        }

        result = ReceivingWorkflowService.save_receiving_flow_draft(
            order_id=self.order_id,
            entries=self.entries(),
            role="storekeeper",
            boxes_raw=json.dumps([box]),
            pallets_raw=json.dumps([pallet]),
            active_box=box["code"],
            active_pallet=pallet["code"],
            user=self.user,
        )

        self.assertEqual(result.status, "saved")
        saved_box = result.payload["flow_state"]["boxes"][0]
        self.assertEqual(saved_box.get("goods_type"), "gv")
        self.assertEqual(saved_box["items"][0].get("goods_type"), "gv")
