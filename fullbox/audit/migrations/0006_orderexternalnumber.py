from django.db import migrations, models
import django.utils.timezone


def _format_sequence_number(raw, suffix, prefixes):
    text = str(raw or "").strip()
    if not text:
        return "-"
    upper = text.upper()
    underscore_suffix = f"_{suffix}"
    if upper.endswith(underscore_suffix):
        number_raw = text[: -len(underscore_suffix)]
        if number_raw.isdigit():
            return f"{int(number_raw)}_{suffix}"
        return f"{number_raw}_{suffix}"
    for prefix in prefixes:
        prefix_upper = prefix.upper()
        for separator in ("-", "_"):
            marker = f"{prefix_upper}{separator}"
            if upper.startswith(marker):
                number_raw = text[len(marker) :]
                if number_raw.isdigit():
                    return f"{int(number_raw)}_{suffix}"
                if number_raw:
                    return f"{number_raw}_{suffix}"
    if text.isdigit():
        return f"{int(text)}_{suffix}"
    return text


def _external_number(order_type, internal_number):
    normalized_type = str(order_type or "").strip().lower()
    raw = str(internal_number or "").strip()
    suffixes = {
        "receiving": ("PR", ("PR",)),
        "processing": ("OBR", ("OBR",)),
        "shipping": ("OTG", ("SO", "OTG")),
        "logistics": ("RS", ("TRIP", "RS")),
        "trip": ("RS", ("TRIP", "RS")),
        "other": ("OTH", ("OTH",)),
        "manual": ("OTH", ("OTH",)),
    }
    if normalized_type in suffixes:
        suffix, prefixes = suffixes[normalized_type]
        return _format_sequence_number(raw, suffix, prefixes)
    return raw or "-"


def _upsert(alias_model, order_type, internal_number):
    normalized_type = str(order_type or "").strip().lower()
    internal = str(internal_number or "").strip()
    if not normalized_type or not internal:
        return
    alias_model.objects.get_or_create(
        order_type=normalized_type,
        internal_number=internal,
        defaults={"external_number": _external_number(normalized_type, internal)},
    )


def seed_external_numbers(apps, schema_editor):
    alias_model = apps.get_model("audit", "OrderExternalNumber")
    order_audit = apps.get_model("audit", "OrderAuditEntry")
    for row in order_audit.objects.values("order_type", "order_id").distinct().iterator():
        _upsert(alias_model, row.get("order_type"), row.get("order_id"))

    try:
        shipping_order = apps.get_model("shipping", "ShippingOrder")
    except LookupError:
        shipping_order = None
    if shipping_order is not None:
        for number in shipping_order.objects.values_list("number", flat=True).iterator():
            _upsert(alias_model, "shipping", number)

    try:
        logistics_trip = apps.get_model("logistics", "LogisticsTrip")
    except LookupError:
        logistics_trip = None
    if logistics_trip is not None:
        for number in logistics_trip.objects.values_list("number", flat=True).iterator():
            _upsert(alias_model, "logistics", number)


class Migration(migrations.Migration):

    dependencies = [
        ("audit", "0005_orderauditentry_order_lookup_idx"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrderExternalNumber",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("order_type", models.CharField(max_length=64)),
                ("internal_number", models.CharField(max_length=128)),
                ("external_number", models.CharField(max_length=128)),
                ("created_at", models.DateTimeField(default=django.utils.timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="orderexternalnumber",
            constraint=models.UniqueConstraint(
                fields=("order_type", "internal_number"),
                name="uniq_order_external_number",
            ),
        ),
        migrations.AddIndex(
            model_name="orderexternalnumber",
            index=models.Index(fields=["order_type", "external_number"], name="audit_ext_number_lookup"),
        ),
        migrations.RunPython(seed_external_numbers, migrations.RunPython.noop),
    ]
