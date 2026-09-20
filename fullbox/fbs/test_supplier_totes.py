from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from sku.models import Agency

from .exceptions import FbsEquipmentError, FbsPickingError
from .models import FbsPickBatch, FbsPickingCart, FbsToteBinding
from .services.equipment import (
    create_supplier_fbs_picking_carts,
    update_supplier_fbs_picking_cart,
)
from .services.picking import _claim_locked_pick_batch


@override_settings(FBS_MODULE_ENABLED=True, FBS_WAREHOUSE_WRITES_ENABLED=True)
class SupplierToteTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="supplier-tote-owner")
        self.other_user = get_user_model().objects.create_user(username="supplier-tote-picker")
        self.agency = Agency.objects.create(agn_name="Supplier tote agency")
        self.other_agency = Agency.objects.create(agn_name="Other supplier tote agency")

    def test_created_tote_is_owned_by_supplier_and_can_be_disabled_while_free(self):
        cart = create_supplier_fbs_picking_carts(
            actor=self.user,
            agency=self.agency,
            count=1,
            name_prefix="Своя тара",
        )[0]
        self.assertEqual(cart.owner_agency, self.agency)
        self.assertTrue(cart.is_active)

        updated = update_supplier_fbs_picking_cart(
            actor=self.user,
            agency=self.agency,
            cart=cart,
            name=cart.name,
            is_active=False,
        )
        self.assertFalse(updated.is_active)

    def test_supplier_cannot_edit_another_supplier_tote(self):
        cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900001",
            name="Чужая тара",
            owner_agency=self.other_agency,
        )
        with self.assertRaisesMessage(FbsEquipmentError, "своей компании"):
            update_supplier_fbs_picking_cart(
                actor=self.user,
                agency=self.agency,
                cart=cart,
                name=cart.name,
                is_active=False,
            )

    def test_busy_tote_cannot_be_disabled(self):
        cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900002",
            name="Занятая тара",
            owner_agency=self.agency,
        )
        FbsToteBinding.objects.create(
            tote=cart,
            state=FbsToteBinding.STATE_PICKING,
            employee=self.other_user,
        )
        with self.assertRaisesMessage(FbsEquipmentError, "сейчас занята"):
            update_supplier_fbs_picking_cart(
                actor=self.user,
                agency=self.agency,
                cart=cart,
                name=cart.name,
                is_active=False,
            )

    def test_supplier_tote_rejects_another_agency_wave(self):
        cart = FbsPickingCart.objects.create(
            barcode="FBS-CART-900003",
            name="Тара поставщика",
            owner_agency=self.agency,
        )
        batch = FbsPickBatch.objects.create(
            agency=self.other_agency,
            status=FbsPickBatch.STATUS_QUEUED,
        )
        with self.assertRaisesMessage(FbsPickingError, "другому поставщику"):
            _claim_locked_pick_batch(batch=batch, actor=self.user, cart=cart)
        batch.refresh_from_db()
        self.assertEqual(batch.status, FbsPickBatch.STATUS_QUEUED)
        self.assertIsNone(batch.assigned_to_id)
