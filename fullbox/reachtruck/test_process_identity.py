"""Отгрузка и перемещение должны различаться на уровне данных, а не догадкой.

Задача reachtruck_process_split_20260920, блок 1.
"""
from django.test import SimpleTestCase, TestCase

from sku.models import Agency

from .models import MoveRequest, MoveTask, resolve_move_request_process
from .services.move_requests import create_batch_move_tasks
from .services.ui_flows import mobile_category_key


# Таблица записана явно, а не через вызов resolve_move_request_process: она же
# продублирована в миграции 0004, и тест обязан ловить расхождение между ними.
RESOLUTION_TABLE = [
    # (context_type, context_id, destination_zone, payload, ожидаемый процесс)
    ("manual", "fbs-movement:94", "OS", {}, "fbs"),
    ("manual", "fbs-floor-movement:12", "OS", {}, "fbs"),
    ("manual", "", "OS", {"fbs_movement_number": "FBS-MOV-000094"}, "fbs"),
    ("manual", "", "OS", {"fbs_replenishment_bridge_v1": True}, "fbs"),
    ("processing", "68", "OBR", {}, "processing"),
    ("manual", "", "OBR", {"processing_order_id": "68"}, "processing"),
    ("receiving", "312", "PR", {}, "receiving"),
    ("manual", "", "PR", {"receiving_order_id": "312"}, "receiving"),
    ("manual", "615", "OTG", {}, "shipping"),
    ("manual", "615", "PR", {"shipping_order_id": "OTG-000615"}, "shipping"),
    ("manual", "615", "PR", {"otg_delivery_request_id": 677}, "shipping"),
    # След происхождения паллеты в задании отгрузки не делает её приёмкой.
    (
        "manual",
        "615",
        "OTG",
        {"shipping_order_id": "OTG-000615", "receiving_order_id": "312"},
        "shipping",
    ),
    ("manual", "", "PR", {}, "manual"),
    ("manual", "", "", {}, "manual"),
]


class ResolveMoveRequestProcessTests(SimpleTestCase):
    def test_resolution_table(self):
        for context_type, context_id, zone, payload, expected in RESOLUTION_TABLE:
            with self.subTest(context_id=context_id, payload=payload):
                self.assertEqual(
                    resolve_move_request_process(
                        context_type=context_type,
                        context_id=context_id,
                        destination_zone=zone,
                        payload=payload,
                    ),
                    expected,
                )

    def test_fbs_wins_over_otg_destination(self):
        """Перемещение ФБС с назначением OTG — это всё равно ФБС, не отгрузка."""
        self.assertEqual(
            resolve_move_request_process(
                context_type="manual",
                context_id="fbs-movement:94",
                destination_zone="OTG",
                payload={"shipping_order_id": "OTG-000615"},
            ),
            "fbs",
        )

    def test_receiving_provenance_does_not_beat_shipping(self):
        """651 из 664 заявок отгрузки на проде несут receiving_order_id как след источника."""
        self.assertEqual(
            resolve_move_request_process(
                context_type="manual",
                context_id="615",
                destination_zone="OTG",
                payload={"receiving_order_id": "312"},
            ),
            "shipping",
        )

    def test_processing_beats_otg_destination(self):
        """На проде есть заявка обработки с назначением OTG — она обязана остаться обработкой."""
        self.assertEqual(
            resolve_move_request_process(
                context_type="processing",
                context_id="68",
                destination_zone="OTG",
                payload={},
            ),
            "processing",
        )

    def test_missing_payload_is_not_an_error(self):
        self.assertEqual(
            resolve_move_request_process(context_type="manual", context_id="", payload=None),
            "manual",
        )


class MobileCategoryKeyTests(SimpleTestCase):
    def test_process_shipping_gives_shipping_category(self):
        move = {"to_location": {"zone": "OS"}}
        self.assertEqual(mobile_category_key(move, {}, request_process="shipping"), "shipping")

    def test_explicit_payload_category_still_wins(self):
        move = {"to_location": {"zone": "OS"}}
        self.assertEqual(
            mobile_category_key(move, {"task_category": "movement"}, request_process="shipping"),
            "movement",
        )

    def test_empty_process_keeps_legacy_heuristic(self):
        """Старые заявки с process='' обязаны вести себя ровно как раньше."""
        cases = [
            ({"to_location": {"zone": "OTG"}}, {}, "shipping"),
            ({"to_location": {"zone": "OS"}}, {"shipping_order_id": "OTG-1"}, "shipping"),
            ({"to_location": {"zone": "OS"}}, {"task_kind_label": "Инвентаризация"}, "inventory"),
            ({"to_location": {"zone": "OS"}}, {"task_kind_label": "Комплектовка"}, "optimization"),
            ({"to_location": {"zone": "OS"}}, {}, "movement"),
        ]
        for move, payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(mobile_category_key(move, payload), expected)
                self.assertEqual(mobile_category_key(move, payload, request_process=""), expected)

    def test_non_shipping_process_does_not_override_heuristic(self):
        move = {"to_location": {"zone": "OS"}}
        self.assertEqual(
            mobile_category_key(move, {"task_kind_label": "Инвентаризация"}, request_process="fbs"),
            "inventory",
        )


class CreateBatchMoveTasksProcessTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(agn_name="ООО Тест разделения процессов")

    def _spec(self, **payload_extra):
        payload = {
            "pallet_code": "TST-0001-000001-gv",
            "from_location": {"zone": "OS", "row": 1, "section": 1, "tier": 1, "cell": 1},
            "to_location": {"zone": "PR", "row": 1, "section": 1, "tier": 1, "cell": 1},
            "requested_qty": 1,
        }
        payload.update(payload_extra)
        return {"description": "Задание ричтрака", "payload": payload}

    def test_process_is_derived_when_not_passed(self):
        move_request, _ids = create_batch_move_tasks(
            context_type="processing",
            context_id="68",
            agency=self.agency,
            task_specs=[self._spec()],
        )
        self.assertEqual(move_request.process, MoveRequest.PROCESS_PROCESSING)

    def test_explicit_process_is_respected(self):
        move_request, _ids = create_batch_move_tasks(
            context_type="manual",
            context_id="",
            agency=self.agency,
            task_specs=[self._spec()],
            process=MoveRequest.PROCESS_FBS,
        )
        self.assertEqual(move_request.process, MoveRequest.PROCESS_FBS)

    def test_otg_destination_is_derived_as_shipping(self):
        move_request, _ids = create_batch_move_tasks(
            context_type="manual",
            context_id="615",
            agency=self.agency,
            destination={"zone": "OTG"},
            task_specs=[self._spec()],
        )
        self.assertEqual(move_request.process, MoveRequest.PROCESS_SHIPPING)

    def test_legacy_lookup_by_context_pair_still_finds_the_request(self):
        """Главное ограничение задачи: поиск по (context_type, context_id) не сломан.

        По этой паре мост ФБС ищет уже созданную заявку. Если поиск перестанет
        находить строку, на живых перемещениях начнут плодиться дубли.
        """
        move_request, _ids = create_batch_move_tasks(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="fbs-movement:94",
            agency=self.agency,
            task_specs=[self._spec()],
            process=MoveRequest.PROCESS_FBS,
        )
        found = MoveRequest.objects.filter(
            context_type=MoveRequest.CONTEXT_MANUAL,
            context_id="fbs-movement:94",
        ).first()
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, move_request.pk)
        self.assertEqual(found.context_type, MoveRequest.CONTEXT_MANUAL)
        self.assertEqual(found.context_id, "fbs-movement:94")


class BackfillRulesTests(TestCase):
    """Бэкофилл читает payload первой задачи заявки."""

    def setUp(self):
        self.agency = Agency.objects.create(agn_name="ООО Тест бэкофилла")

    def _request_with_task(self, *, context_type, context_id, zone, payload):
        request = MoveRequest.objects.create(
            context_type=context_type,
            context_id=context_id,
            agency=self.agency,
            destination_zone=zone,
        )
        MoveTask.objects.create(
            request=request,
            pallet_code="TST-0001-000001-gv",
            to_zone=zone,
            payload=payload,
            legacy_order_id=f"STM-TEST-{request.pk}",
        )
        return request

    def test_rules_match_the_table_for_stored_rows(self):
        for context_type, context_id, zone, payload, expected in RESOLUTION_TABLE:
            with self.subTest(context_id=context_id, payload=payload):
                request = self._request_with_task(
                    context_type=context_type,
                    context_id=context_id,
                    zone=zone or "PR",
                    payload=payload,
                )
                resolved = resolve_move_request_process(
                    context_type=request.context_type,
                    context_id=request.context_id,
                    destination_zone=request.destination_zone,
                    payload=request.tasks.order_by("id").first().payload,
                )
                self.assertEqual(resolved, expected)

    def test_request_without_tasks_falls_back_to_context(self):
        request = MoveRequest.objects.create(
            context_type="receiving",
            context_id="312",
            agency=self.agency,
            destination_zone="PR",
        )
        self.assertFalse(request.tasks.exists())
        self.assertEqual(
            resolve_move_request_process(
                context_type=request.context_type,
                context_id=request.context_id,
                destination_zone=request.destination_zone,
                payload={},
            ),
            MoveRequest.PROCESS_RECEIVING,
        )
