from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import logistics.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("employees", "0011_employee_driver_role"),
        ("logistics", "0007_carriervehicle"),
    ]

    operations = [
        migrations.AddField(
            model_name="logisticstrip",
            name="assigned_driver",
            field=models.ForeignKey(blank=True, limit_choices_to={"role": "driver"}, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_delivery_trips", to="employees.employee", verbose_name="Водитель в личном кабинете"),
        ),
        migrations.AddField(
            model_name="logisticstrip",
            name="driver_assigned_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Водитель назначен"),
        ),
        migrations.AddField(
            model_name="logisticstrip",
            name="driver_assigned_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="delivery_driver_assignments", to="employees.employee", verbose_name="Кто назначил водителя"),
        ),
        migrations.AddField(
            model_name="logisticstrip",
            name="driver_status",
            field=models.CharField(choices=[("unassigned", "Водитель не назначен"), ("assigned", "Водитель назначен"), ("accepted", "Рейс принят водителем"), ("en_route", "Водитель выехал"), ("awaiting_delivery", "Ожидает сдачи"), ("delivered", "Груз сдан"), ("problem", "Проблемный рейс")], db_index=True, default="unassigned", max_length=32, verbose_name="Статус водителя"),
        ),
        migrations.AddField(model_name="logisticstrip", name="scheduled_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Плановая подача")),
        migrations.AddField(model_name="logisticstrip", name="driver_accepted_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Рейс принят")),
        migrations.AddField(model_name="logisticstrip", name="driver_departed_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Водитель выехал")),
        migrations.AddField(model_name="logisticstrip", name="driver_arrived_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Водитель прибыл")),
        migrations.AddField(model_name="logisticstrip", name="actual_handover_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Фактическая сдача")),
        migrations.AddField(model_name="logisticstrip", name="handover_system_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Системное время подтверждения")),
        migrations.AddField(model_name="logisticstrip", name="handover_time_change_reason", field=models.TextField(blank=True, verbose_name="Причина изменения времени сдачи")),
        migrations.AddField(model_name="logisticstrip", name="gate_number", field=models.CharField(blank=True, max_length=64, verbose_name="Номер ворот")),
        migrations.AddField(model_name="logisticstrip", name="waiting_started_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Начало ожидания")),
        migrations.AddField(model_name="logisticstrip", name="unloading_finished_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Окончание разгрузки")),
        migrations.AddField(model_name="logisticstrip", name="delivery_receipt_number", field=models.CharField(blank=True, max_length=128, verbose_name="Номер документа приемки")),
        migrations.AddField(model_name="logisticstrip", name="driver_result_comment", field=models.TextField(blank=True, verbose_name="Комментарий водителя")),
        migrations.AddField(model_name="logisticstrip", name="closed_at", field=models.DateTimeField(blank=True, null=True, verbose_name="Рейс закрыт")),
        migrations.CreateModel(
            name="ProblemTrip",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason_code", models.CharField(choices=[("warehouse_rejects", "Склад не принимает поставку"), ("late_slot", "Опоздание на временной слот"), ("document_error", "Ошибка в документах"), ("bad_barcode", "Неверный штрихкод"), ("marking_error", "Ошибка в маркировке или КИЗах"), ("packaging", "Неправильная упаковка"), ("quantity", "Расхождение по количеству"), ("damage", "Повреждение товара"), ("no_marketplace_request", "Отсутствие заявки в системе маркетплейса"), ("wrong_address", "Неправильный адрес или склад"), ("queue_closed", "Очередь и завершение времени приемки"), ("marketplace_tech", "Техническая ошибка маркетплейса"), ("employee_refusal", "Отказ сотрудника склада"), ("transport", "Проблема с транспортом"), ("driver", "Проблема по вине водителя"), ("client", "Проблема по вине клиента"), ("other", "Другая причина")], db_index=True, max_length=40, verbose_name="Причина несдачи")),
                ("comment", models.TextField(verbose_name="Подробный комментарий")),
                ("refusal_at", models.DateTimeField(verbose_name="Дата и время отказа")),
                ("rejecting_employee", models.CharField(blank=True, max_length=255, verbose_name="Кто отказал в приемке")),
                ("gate_number", models.CharField(blank=True, max_length=64, verbose_name="Номер ворот")),
                ("support_case_number", models.CharField(blank=True, max_length=128, verbose_name="Обращение в поддержку")),
                ("responsible_party", models.CharField(choices=[("unknown", "Не определена"), ("marketplace", "Маркетплейс"), ("client", "Клиент"), ("driver", "Водитель"), ("carrier", "Перевозчик"), ("fullbox", "Fullbox")], db_index=True, default="unknown", max_length=24, verbose_name="Ответственная сторона")),
                ("status", models.CharField(choices=[("new", "Новый"), ("review", "На проверке"), ("wait_marketplace", "Ожидает ответа маркетплейса"), ("redelivery", "Назначена повторная доставка"), ("penalty_confirmed", "Штраф подтвержден"), ("penalty_disputed", "Штраф оспаривается"), ("resolved", "Проблема решена"), ("closed", "Закрыт")], db_index=True, default="new", max_length=32, verbose_name="Статус разбирательства")),
                ("next_action_at", models.DateTimeField(blank=True, null=True, verbose_name="Дата следующего действия")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создан")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлен")),
                ("assigned_to", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="assigned_problem_trips", to="employees.employee", verbose_name="Ответственный сотрудник")),
                ("trip", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="problem", to="logistics.logisticstrip", verbose_name="Рейс")),
            ],
            options={"verbose_name": "Проблемный рейс", "verbose_name_plural": "Проблемные рейсы", "ordering": ("-created_at", "-id")},
        ),
        migrations.CreateModel(
            name="TripAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attachment_type", models.CharField(choices=[("gate", "Фотография ворот"), ("receipt", "Документ о приемке"), ("problem", "Подтверждение проблемы"), ("document", "Документ рейса")], max_length=24, verbose_name="Тип")),
                ("file", models.FileField(upload_to=logistics.models.trip_attachment_upload_to, verbose_name="Файл")),
                ("photo_created_at", models.DateTimeField(blank=True, null=True, verbose_name="Время создания фото")),
                ("latitude", models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True, verbose_name="Широта")),
                ("longitude", models.DecimalField(blank=True, decimal_places=6, max_digits=9, null=True, verbose_name="Долгота")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Загружено")),
                ("trip", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="driver_attachments", to="logistics.logisticstrip", verbose_name="Рейс")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_trip_attachments", to=settings.AUTH_USER_MODEL, verbose_name="Загрузил")),
            ],
            options={"verbose_name": "Файл рейса", "verbose_name_plural": "Файлы рейсов", "ordering": ("created_at", "id")},
        ),
        migrations.CreateModel(
            name="TripHistory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("event_type", models.CharField(db_index=True, max_length=64, verbose_name="Тип события")),
                ("description", models.TextField(verbose_name="Описание")),
                ("payload", models.JSONField(blank=True, default=dict, verbose_name="Данные")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Когда")),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="trip_history_entries", to=settings.AUTH_USER_MODEL, verbose_name="Пользователь")),
                ("trip", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="driver_history", to="logistics.logisticstrip", verbose_name="Рейс")),
            ],
            options={"verbose_name": "История рейса", "verbose_name_plural": "История рейсов", "ordering": ("-created_at", "-id")},
        ),
        migrations.CreateModel(
            name="TripNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=255, verbose_name="Заголовок")),
                ("text", models.TextField(verbose_name="Текст")),
                ("detail_url", models.CharField(max_length=500, verbose_name="Ссылка")),
                ("source_key", models.CharField(max_length=160, unique=True, verbose_name="Ключ события")),
                ("read_at", models.DateTimeField(blank=True, null=True, verbose_name="Прочитано")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Создано")),
                ("recipient", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="trip_notifications", to="employees.employee", verbose_name="Получатель")),
                ("trip", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to="logistics.logisticstrip", verbose_name="Рейс")),
            ],
            options={"verbose_name": "Уведомление о рейсе", "verbose_name_plural": "Уведомления о рейсах", "ordering": ("-created_at", "-id")},
        ),
        migrations.CreateModel(
            name="TripPenalty",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("marketplace_name", models.CharField(max_length=128, verbose_name="Маркетплейс")),
                ("penalty_type", models.CharField(max_length=128, verbose_name="Тип штрафа")),
                ("reason", models.TextField(blank=True, verbose_name="Причина штрафа")),
                ("charged_at", models.DateField(blank=True, null=True, verbose_name="Дата начисления")),
                ("estimated_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Предполагаемая сумма")),
                ("actual_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Фактическая сумма")),
                ("document_number", models.CharField(blank=True, max_length=128, verbose_name="Номер документа")),
                ("support_reference", models.CharField(blank=True, max_length=255, verbose_name="Ссылка или номер обращения")),
                ("responsible_party", models.CharField(choices=[("unknown", "Не определена"), ("marketplace", "Маркетплейс"), ("client", "Клиент"), ("driver", "Водитель"), ("carrier", "Перевозчик"), ("fullbox", "Fullbox")], default="unknown", max_length=24, verbose_name="Ответственная сторона")),
                ("status", models.CharField(choices=[("expected", "Ожидается"), ("charged", "Начислен"), ("review", "На проверке"), ("disputed", "Оспаривается"), ("canceled", "Отменен"), ("confirmed", "Подтвержден"), ("paid", "Оплачен")], db_index=True, default="expected", max_length=24, verbose_name="Статус штрафа")),
                ("comment", models.TextField(blank=True, verbose_name="Комментарий")),
                ("document", models.FileField(blank=True, upload_to=logistics.models.trip_attachment_upload_to, verbose_name="Документ")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создан")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Обновлен")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_trip_penalties", to=settings.AUTH_USER_MODEL, verbose_name="Создал")),
                ("problem", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="penalties", to="logistics.problemtrip", verbose_name="Проблемный рейс")),
            ],
            options={"verbose_name": "Штраф рейса", "verbose_name_plural": "Штрафы рейсов", "ordering": ("-created_at", "-id")},
        ),
    ]
