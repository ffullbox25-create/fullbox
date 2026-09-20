from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from audit.models import OrderAuditEntry
from client_cabinet.client_drafts import (
    CLIENT_DRAFT_LIMIT,
    close_client_drafts_after_submit,
    find_client_draft,
    is_draft_payload,
    list_client_draft_order_ids,
    supersede_extra_client_drafts,
)
from client_cabinet.models import AgencyPortalMember, OtherRequest
from client_cabinet.portal_access import ROLE_ADMIN, ROLE_MANAGER, sections_for_role
from client_cabinet.request_ownership import (
    portal_request_owner_user_id,
    portal_user_can_edit_request,
)
from shipping.models import ShippingOrder
from sku.models import Agency


User = get_user_model()
urlpatterns = []


@override_settings(ROOT_URLCONF=__name__)
class PortalRequestOwnershipTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="company-owner", password="pwd")
        self.manager_a = User.objects.create_user(username="company-manager-a", password="pwd")
        self.manager_b = User.objects.create_user(username="company-manager-b", password="pwd")
        self.portal_admin = User.objects.create_user(username="company-admin", password="pwd")
        self.agency = Agency.objects.create(
            agn_name="Клиент с сотрудниками",
            portal_user=self.owner,
        )
        self.other_agency = Agency.objects.create(agn_name="Другой клиент")
        self._member(self.manager_a, ROLE_MANAGER)
        self._member(self.manager_b, ROLE_MANAGER)
        self._member(self.portal_admin, ROLE_ADMIN)

    def _member(self, user, role):
        return AgencyPortalMember.objects.create(
            agency=self.agency,
            user=user,
            last_name=user.username,
            first_name="Тест",
            email=f"{user.username}@example.test",
            role=role,
            sections=sections_for_role(role),
            is_active=True,
            created_by=self.owner,
        )

    def _request(self, user):
        request = RequestFactory().get("/client/dashboard/lk/")
        request.user = user
        request.session = {}
        return request

    def _audit(self, *, order_type, order_id, user, agency=None, status="draft"):
        return OrderAuditEntry.objects.create(
            agency=agency or self.agency,
            order_type=order_type,
            order_id=order_id,
            action="create",
            user=user,
            description="Черновик",
            payload={
                "status": status,
                "status_label": "Черновик" if status == "draft" else status,
                "submit_action": "draft" if status == "draft" else status,
            },
        )

    def test_owner_is_resolved_for_all_request_types(self):
        shipping = ShippingOrder.objects.create(
            number="OTG-OWNER-A",
            agency=self.agency,
            created_by=self.manager_a,
            status=ShippingOrder.STATUS_DRAFT,
        )
        other = OtherRequest.objects.create(
            public_number="OTH-OWNER-A",
            agency=self.agency,
            created_by=self.manager_a,
            status=OtherRequest.STATUS_DRAFT,
        )
        self._audit(order_type="receiving", order_id="PR-OWNER-A", user=self.manager_a)
        self._audit(order_type="processing", order_id="OBR-OWNER-A", user=self.manager_a)

        self.assertEqual(
            portal_request_owner_user_id(
                agency=self.agency,
                order_type="shipping",
                order_id=shipping.number,
            ),
            self.manager_a.id,
        )
        self.assertEqual(
            portal_request_owner_user_id(
                agency=self.agency,
                order_type="other",
                order_id=other.public_number,
            ),
            self.manager_a.id,
        )
        self.assertEqual(
            portal_request_owner_user_id(
                agency=self.agency,
                order_type="receiving",
                order_id="PR-OWNER-A",
            ),
            self.manager_a.id,
        )
        self.assertEqual(
            portal_request_owner_user_id(
                agency=self.agency,
                order_type="packing",
                order_id="OBR-OWNER-A",
            ),
            self.manager_a.id,
        )

    def test_company_draft_is_shared_but_submitted_request_keeps_owner(self):
        self._audit(order_type="receiving", order_id="PR-ACCESS-A", user=self.manager_a)

        self.assertTrue(
            portal_user_can_edit_request(
                user=self.manager_a,
                agency=self.agency,
                order_type="receiving",
                order_id="PR-ACCESS-A",
                request=self._request(self.manager_a),
            )
        )
        self.assertTrue(
            portal_user_can_edit_request(
                user=self.manager_b,
                agency=self.agency,
                order_type="receiving",
                order_id="PR-ACCESS-A",
                request=self._request(self.manager_b),
            )
        )
        self._audit(
            order_type="receiving",
            order_id="PR-ACCESS-A",
            user=self.manager_a,
            status="submitted",
        )
        self.assertFalse(
            portal_user_can_edit_request(
                user=self.manager_b,
                agency=self.agency,
                order_type="receiving",
                order_id="PR-ACCESS-A",
                request=self._request(self.manager_b),
            )
        )
        for admin_user in (self.owner, self.portal_admin):
            self.assertTrue(
                portal_user_can_edit_request(
                    user=admin_user,
                    agency=self.agency,
                    order_type="receiving",
                    order_id="PR-ACCESS-A",
                    request=self._request(admin_user),
                )
            )

    def test_same_request_number_in_other_company_is_not_used_as_owner(self):
        self._audit(
            order_type="receiving",
            order_id="PR-SAME-NUMBER",
            user=self.manager_a,
            agency=self.other_agency,
        )

        self.assertIsNone(
            portal_request_owner_user_id(
                agency=self.agency,
                order_type="receiving",
                order_id="PR-SAME-NUMBER",
            )
        )
        self.assertFalse(
            portal_user_can_edit_request(
                user=self.manager_a,
                agency=self.agency,
                order_type="receiving",
                order_id="PR-SAME-NUMBER",
                request=self._request(self.manager_a),
            )
        )

    def test_one_draft_per_type_is_scoped_to_company(self):
        self.assertEqual(CLIENT_DRAFT_LIMIT, 1)
        self._audit(order_type="processing", order_id="draft-a", user=self.manager_a)
        self._audit(order_type="processing", order_id="draft-b", user=self.manager_b)

        self.assertEqual(
            list_client_draft_order_ids(
                agency=self.agency,
                order_type="processing",
                user=self.manager_a,
            ),
            ["draft-b", "draft-a"],
        )
        self.assertEqual(
            list_client_draft_order_ids(
                agency=self.agency,
                order_type="processing",
                user=self.manager_b,
            ),
            ["draft-b", "draft-a"],
        )
        self.assertEqual(
            find_client_draft(
                agency=self.agency,
                order_type="processing",
                user=self.manager_a,
            )["order_id"],
            "draft-b",
        )

        superseded = supersede_extra_client_drafts(
            agency=self.agency,
            order_type="processing",
            keep_order_id="draft-b",
            user=self.manager_b,
        )
        self.assertEqual(superseded, 1)
        self.assertEqual(
            list_client_draft_order_ids(
                agency=self.agency,
                order_type="processing",
                user=self.manager_a,
            ),
            ["draft-b"],
        )

        closed = close_client_drafts_after_submit(
            agency=self.agency,
            order_type="processing",
            submitted_order_id="OBR-SENT-A",
            user=self.manager_a,
        )

        self.assertEqual(closed, 1)
        latest_a = OrderAuditEntry.objects.filter(
            agency=self.agency,
            order_type="processing",
            order_id="draft-a",
        ).order_by("-created_at", "-id").first()
        latest_b = OrderAuditEntry.objects.filter(
            agency=self.agency,
            order_type="processing",
            order_id="draft-b",
        ).order_by("-created_at", "-id").first()
        self.assertFalse(is_draft_payload(latest_a.payload))
        self.assertFalse(is_draft_payload(latest_b.payload))
