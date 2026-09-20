from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("shipping", "0008_alter_shippingorder_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="ShippingTransportNote",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("document_number", models.CharField(blank=True, max_length=64, verbose_name="Номер ТН")),
                ("document_date", models.DateField(blank=True, null=True, verbose_name="Дата ТН")),
                ("shipper_name", models.CharField(blank=True, max_length=255, verbose_name="Грузоотправитель")),
                ("shipper_inn", models.CharField(blank=True, max_length=32, verbose_name="ИНН грузоотправителя")),
                ("shipper_address", models.TextField(blank=True, verbose_name="Адрес грузоотправителя")),
                ("shipper_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон грузоотправителя")),
                ("consignee_name", models.CharField(blank=True, max_length=255, verbose_name="Грузополучатель")),
                ("consignee_inn", models.CharField(blank=True, max_length=32, verbose_name="ИНН грузополучателя")),
                ("consignee_address", models.TextField(blank=True, verbose_name="Адрес грузополучателя")),
                ("consignee_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон грузополучателя")),
                ("carrier_name", models.CharField(blank=True, max_length=255, verbose_name="Перевозчик")),
                ("carrier_inn", models.CharField(blank=True, max_length=32, verbose_name="ИНН перевозчика")),
                ("carrier_address", models.TextField(blank=True, verbose_name="Адрес перевозчика")),
                ("carrier_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон перевозчика")),
                ("loading_address", models.TextField(blank=True, verbose_name="Адрес погрузки")),
                ("unloading_address", models.TextField(blank=True, verbose_name="Адрес выгрузки")),
                ("cargo_name", models.TextField(blank=True, verbose_name="Наименование груза")),
                ("cargo_package_count", models.PositiveIntegerField(default=0, verbose_name="Количество мест")),
                ("cargo_package_type", models.CharField(blank=True, max_length=64, verbose_name="Тип мест")),
                ("cargo_weight_kg", models.DecimalField(blank=True, decimal_places=3, max_digits=10, null=True, verbose_name="Вес, кг")),
                ("cargo_declared_value", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name="Объявленная стоимость")),
                ("accompanying_documents", models.TextField(blank=True, verbose_name="Сопроводительные документы")),
                ("special_instructions", models.TextField(blank=True, verbose_name="Указания грузоотправителя")),
                ("transportation_conditions", models.TextField(blank=True, verbose_name="Условия перевозки")),
                ("delivery_notes", models.TextField(blank=True, verbose_name="Оговорки и замечания")),
                ("driver_name", models.CharField(blank=True, max_length=255, verbose_name="Водитель")),
                ("driver_phone", models.CharField(blank=True, max_length=32, verbose_name="Телефон водителя")),
                ("vehicle_number", models.CharField(blank=True, max_length=32, verbose_name="Номер автомобиля")),
                ("trailer_number", models.CharField(blank=True, max_length=32, verbose_name="Номер прицепа")),
                ("service_cost", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True, verbose_name="Стоимость услуг")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлено")),
                (
                    "order",
                    models.OneToOneField(
                        on_delete=models.deletion.CASCADE,
                        related_name="transport_note",
                        to="shipping.shippingorder",
                        verbose_name="Заявка",
                    ),
                ),
            ],
            options={
                "verbose_name": "Транспортная накладная",
                "verbose_name_plural": "Транспортные накладные",
                "ordering": ["-updated_at", "-id"],
            },
        ),
    ]
