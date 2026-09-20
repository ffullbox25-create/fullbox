from django.test import SimpleTestCase

from billing.payment_qr import build_payment_qr_payload


class PaymentQrTests(SimpleTestCase):
    def test_st00012_payload(self):
        payload = build_payment_qr_payload(
            name='ООО "ФуллБокс"',
            personal_acc="40702810801500155514",
            bank_name='ООО "Банк Точка"',
            bic="044525104",
            corresp_acc="30101810745374525104",
            payee_inn="5001149130",
            purpose="Оплата по заказу клиента №701",
            amount="13460.00",
            kpp="503101001",
        )
        self.assertTrue(payload.startswith("ST00012|"))
        self.assertIn("PersonalAcc=40702810801500155514", payload)
        self.assertIn("BIC=044525104", payload)
        self.assertIn("Sum=1346000", payload)
        self.assertIn("KPP=503101001", payload)
        self.assertIn("Purpose=Оплата по заказу клиента №701", payload)

    def test_missing_requisites_returns_empty(self):
        self.assertEqual(
            build_payment_qr_payload(
                name="X",
                personal_acc="",
                bank_name="Bank",
                bic="044525104",
                corresp_acc="30101810745374525104",
                payee_inn="5001149130",
                purpose="Оплата",
                amount="100",
            ),
            "",
        )
