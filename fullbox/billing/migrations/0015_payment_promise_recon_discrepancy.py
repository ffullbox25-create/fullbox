import billing.models
import django.db.models.deletion
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0014_document_review_and_upd"),
        ("employees", "0001_initial"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentPromise",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14, verbose_name="Сумма")),
                ("promised_date", models.DateField(db_index=True, verbose_name="Обещанная дата")),
                ("contact_person", models.CharField(blank=True, max_length=255, verbose_name="Контактное лицо")),
                ("client_comment", models.TextField(blank=True, verbose_name="Комментарий клиента")),
                ("manager_comment", models.TextField(blank=True, verbose_name="Комментарий менеджера")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Ожидается"),
                            ("fulfilled", "Выполнено"),
                            ("partial", "Частично выполнено"),
                            ("overdue", "Просрочено"),
                            ("cancelled", "Отменено"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=32,
                        verbose_name="Статус",
                    ),
                ),
                ("result_note", models.TextField(blank=True, verbose_name="Фактический результат")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_promises",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_payment_promises",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="payment_promises",
                        to="billing.clientinvoice",
                        verbose_name="Счёт",
                    ),
                ),
                (
                    "manager",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="payment_promises",
                        to="employees.employee",
                        verbose_name="Менеджер",
                    ),
                ),
            ],
            options={
                "verbose_name": "Обещание оплаты",
                "verbose_name_plural": "Обещания оплаты",
                "ordering": ["promised_date", "-id"],
            },
        ),
        migrations.CreateModel(
            name="ReconciliationRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("period_from", models.DateField(verbose_name="Период с")),
                ("period_to", models.DateField(verbose_name="Период по")),
                (
                    "opening_balance",
                    models.DecimalField(
                        decimal_places=2, default=Decimal("0.00"), max_digits=14, verbose_name="Начальное сальдо"
                    ),
                ),
                (
                    "closing_balance",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Конечное сальдо"
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Черновик"),
                            ("submitted", "Передан бухгалтеру"),
                            ("formed", "Сформирован"),
                            ("sent", "Отправлен клиенту"),
                            ("confirmed", "Подтверждён"),
                            ("disputed", "Есть расхождения"),
                            ("signed", "Подписан"),
                            ("archived", "Архивный"),
                        ],
                        db_index=True,
                        default="draft",
                        max_length=32,
                        verbose_name="Статус",
                    ),
                ),
                ("manager_comment", models.TextField(blank=True, verbose_name="Комментарий менеджера")),
                ("accountant_comment", models.TextField(blank=True, verbose_name="Комментарий бухгалтера")),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="reconciliation_requests",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_reconciliation_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Акт сверки (запрос)",
                "verbose_name_plural": "Акты сверки (запросы)",
                "ordering": ["-period_to", "-id"],
            },
        ),
        migrations.CreateModel(
            name="BillingDiscrepancy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "discrepancy_type",
                    models.CharField(
                        choices=[
                            ("quantity", "Количество"),
                            ("service", "Услуга"),
                            ("price", "Цена"),
                            ("missing_operation", "Нет операции"),
                            ("duplicate", "Дубль"),
                            ("period", "Период"),
                            ("document", "Документ"),
                            ("reconciliation", "Акт сверки"),
                            ("payment", "Оплата"),
                            ("other", "Прочее"),
                        ],
                        default="other",
                        max_length=32,
                        verbose_name="Тип",
                    ),
                ),
                ("description", models.TextField(verbose_name="Описание")),
                (
                    "disputed_amount",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=14, null=True, verbose_name="Сумма спора"
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("new", "Новое"),
                            ("manager_review", "На проверке у менеджера"),
                            ("accountant_review", "На проверке у бухгалтера"),
                            ("waiting_client", "Ожидает клиента"),
                            ("confirmed", "Подтверждено"),
                            ("rejected", "Отклонено"),
                            ("needs_correction", "Требует корректировки"),
                            ("closed", "Закрыто"),
                        ],
                        db_index=True,
                        default="new",
                        max_length=32,
                        verbose_name="Статус",
                    ),
                ),
                ("manager_comment", models.TextField(blank=True, verbose_name="Комментарий менеджера")),
                ("result_note", models.TextField(blank=True, verbose_name="Результат")),
                (
                    "attachment",
                    models.FileField(
                        blank=True, null=True, upload_to=billing.models.billing_document_upload_to, verbose_name="Файл"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "act",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="discrepancies",
                        to="billing.billingact",
                    ),
                ),
                (
                    "act_dispute",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="discrepancies",
                        to="billing.billingactdispute",
                    ),
                ),
                (
                    "application",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="discrepancies",
                        to="billing.billingapplication",
                    ),
                ),
                (
                    "charge",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="discrepancies",
                        to="billing.applicationcharge",
                    ),
                ),
                (
                    "client",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="billing_discrepancies",
                        to="sku.agency",
                        verbose_name="Клиент",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="created_billing_discrepancies",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="discrepancies",
                        to="billing.clientinvoice",
                    ),
                ),
            ],
            options={
                "verbose_name": "Расхождение биллинга",
                "verbose_name_plural": "Расхождения биллинга",
                "ordering": ["-created_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="paymentpromise",
            index=models.Index(fields=["client", "status"], name="billing_pay_client__7a1e01_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentpromise",
            index=models.Index(fields=["status", "promised_date"], name="billing_pay_status_8f2e01_idx"),
        ),
        migrations.AddIndex(
            model_name="reconciliationrequest",
            index=models.Index(fields=["client", "status"], name="billing_rec_client__9b1e01_idx"),
        ),
        migrations.AddIndex(
            model_name="reconciliationrequest",
            index=models.Index(fields=["status", "period_to"], name="billing_rec_status_0c2e01_idx"),
        ),
        migrations.AddIndex(
            model_name="billingdiscrepancy",
            index=models.Index(fields=["client", "status"], name="billing_dis_client__1d1e01_idx"),
        ),
        migrations.AddIndex(
            model_name="billingdiscrepancy",
            index=models.Index(fields=["status", "created_at"], name="billing_dis_status_2e2e01_idx"),
        ),
    ]
