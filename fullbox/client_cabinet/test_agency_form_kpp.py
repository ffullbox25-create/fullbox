from django.test import TestCase

from sku.models import Agency

from .forms import AgencyForm


class AgencyFormKppTests(TestCase):
    def setUp(self):
        self.agency = Agency.objects.create(
            agn_name="Индивидуальный предприниматель Опря Сергей Николаевич",
            short_name="ИП Опря С. Н.",
            pref="OSN",
            inn="401109329251",
            kpp="None",
            ogrn="316500100060095",
            phone="+7 (111) 111-11-11",
            email="serj872004@mail.ru",
            adres="Удмуртская Республика",
            fakt_adres="Удмуртская Республика",
            fio_agn="Сергей",
        )

    def form_data(self):
        return {
            "agn_name": self.agency.agn_name,
            "short_name": self.agency.short_name,
            "pref": self.agency.pref,
            "inn": self.agency.inn,
            "kpp": "None",
            "ogrn": self.agency.ogrn,
            "phone": self.agency.phone,
            "email": self.agency.email,
            "adres": self.agency.adres,
            "fakt_adres": self.agency.fakt_adres,
            "fio_agn": self.agency.fio_agn,
            "contract_numb": "",
            "contract_link": "",
            "portal_login": "",
            "portal_password": "",
        }

    def test_legacy_none_kpp_is_rendered_as_empty(self):
        form = AgencyForm(instance=self.agency)

        self.assertEqual(form["kpp"].value(), "")

    def test_disabled_legacy_none_kpp_does_not_block_client_update(self):
        form = AgencyForm(data=self.form_data(), instance=self.agency)
        form.fields["kpp"].disabled = True

        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.cleaned_data["kpp"])
        saved = form.save()
        self.assertIsNone(saved.kpp)

    def test_real_kpp_is_preserved(self):
        self.agency.kpp = "500101001"
        self.agency.save(update_fields=["kpp"])
        data = self.form_data()
        data["kpp"] = "500101001"
        form = AgencyForm(data=data, instance=self.agency)

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["kpp"], "500101001")
