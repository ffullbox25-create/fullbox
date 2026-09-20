from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from employees.models import Employee

from .exceptions import FbsEquipmentError
from .models import FbsPickingCart, FbsToteBinding
from .services.equipment import create_fbs_picking_carts, update_fbs_picking_cart


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class StorekeeperToteSettingsTests(TestCase):
    def setUp(self):
        users = get_user_model()
        self.storekeeper = users.objects.create_user(username="tote-settings-storekeeper")
        Employee.objects.create(
            full_name="Кладовщик тары",
            user=self.storekeeper,
            role="storekeeper",
            is_active=True,
        )
        self.outsider = users.objects.create_user(username="tote-settings-outsider")

    def test_storekeeper_creates_global_tote_and_opens_settings_page(self):
        cart = create_fbs_picking_carts(
            actor=self.storekeeper,
            count=1,
            name_prefix="Тестовая тара",
        )[0]
        self.assertIsNone(cart.owner_agency_id)
        self.assertTrue(cart.barcode.startswith("FBS-CART-"))

        self.client.force_login(self.storekeeper)
        response = self.client.get(reverse("fbs:operator_totes"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, cart.barcode)
        self.assertContains(response, "Кладовщик тары")

    def test_non_warehouse_user_cannot_create_tote(self):
        with self.assertRaisesMessage(FbsEquipmentError, "только кладовщику"):
            create_fbs_picking_carts(actor=self.outsider, count=1)

    def test_storekeeper_can_disable_free_tote(self):
        cart = create_fbs_picking_carts(actor=self.storekeeper, count=1)[0]
        updated = update_fbs_picking_cart(
            actor=self.storekeeper,
            cart=cart,
            name=cart.name,
            is_active=False,
        )
        self.assertFalse(updated.is_active)

    def test_storekeeper_cannot_disable_busy_tote(self):
        cart = create_fbs_picking_carts(actor=self.storekeeper, count=1)[0]
        FbsToteBinding.objects.create(
            tote=cart,
            state=FbsToteBinding.STATE_PICKING,
            employee=self.storekeeper,
        )
        with self.assertRaisesMessage(FbsEquipmentError, "сейчас занята"):
            update_fbs_picking_cart(
                actor=self.storekeeper,
                cart=cart,
                name=cart.name,
                is_active=False,
            )

    def test_supplier_portal_has_no_tote_management_route(self):
        self.client.force_login(self.storekeeper)
        response = self.client.get("/client/api/v1/fbs/totes/")
        self.assertEqual(response.status_code, 404)
