import django.db.models.deletion
import warehouse_goods.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("billing", "0027_fbs_billing_contour"),
        ("sku", "0010_sku_weight_gross_kg_sku_weight_net_kg"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="GoodsExtraFieldDefinition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=128, verbose_name="Название")),
                ("slug", models.SlugField(max_length=96, unique=True, verbose_name="Код")),
                ("field_type", models.CharField(choices=[("text", "Текст"), ("number", "Число"), ("boolean", "Да/нет"), ("date", "Дата")], default="text", max_length=16, verbose_name="Тип")),
                ("is_active", models.BooleanField(default=True, verbose_name="Активно")),
                ("sort_order", models.PositiveIntegerField(default=100, verbose_name="Порядок")),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="GoodsProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("internal_notes", models.TextField(blank=True, verbose_name="Внутренние заметки")),
                ("fbs_shelf_life_required", models.BooleanField(default=False, verbose_name="Контролировать срок годности FBS")),
                ("receiving_task_template", models.TextField(blank=True, verbose_name="Типовая задача на приемку")),
                ("shipping_task_template", models.TextField(blank=True, verbose_name="Типовая задача на отгрузку")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("sku", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_goods_profile", to="sku.sku")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_goods_profiles", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "Складской профиль товара", "verbose_name_plural": "Складские профили товаров"},
        ),
        migrations.CreateModel(
            name="GoodsActionAudit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=64)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_goods_actions", to="sku.sku")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="warehouse_goods_actions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-id"], "indexes": [models.Index(fields=["sku", "-created_at"], name="warehouse_g_sku_id_db2df8_idx")]},
        ),
        migrations.CreateModel(
            name="GoodsDefaultService",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stage", models.CharField(choices=[("receiving", "Приемка"), ("processing", "Обработка"), ("shipping", "Отгрузка"), ("full_cycle", "Полный цикл")], max_length=24, verbose_name="Этап")),
                ("unit_price", models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True, verbose_name="Цена")),
                ("quantity", models.DecimalField(decimal_places=3, default=1, max_digits=12, verbose_name="Количество")),
                ("auto_add", models.BooleanField(default=True, verbose_name="Добавлять автоматически")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("service", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="goods_defaults", to="billing.billingservice")),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_default_services", to="sku.sku")),
            ],
            options={"ordering": ["stage", "service__sort_order", "service__name"], "indexes": [models.Index(fields=["sku", "stage"], name="warehouse_g_sku_id_b5e763_idx")], "constraints": [models.UniqueConstraint(fields=("sku", "stage", "service"), name="uniq_goods_default_service")]},
        ),
        migrations.CreateModel(
            name="GoodsExtraFieldValue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("value", models.JSONField(blank=True, null=True, verbose_name="Значение")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("definition", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="values", to="warehouse_goods.goodsextrafielddefinition")),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_goods_extra_values", to="sku.sku")),
                ("updated_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="updated_goods_extra_values", to=settings.AUTH_USER_MODEL)),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("sku", "definition"), name="uniq_goods_extra_value")]},
        ),
        migrations.CreateModel(
            name="GoodsFile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to=warehouse_goods.models.goods_file_upload_to, verbose_name="Файл")),
                ("original_name", models.CharField(max_length=255, verbose_name="Имя файла")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True)),
                ("sku", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="warehouse_goods_files", to="sku.sku")),
                ("uploaded_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="uploaded_goods_files", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-uploaded_at", "-id"], "indexes": [models.Index(fields=["sku", "-uploaded_at"], name="warehouse_g_sku_id_e501d7_idx")]},
        ),
    ]
