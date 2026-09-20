import re

from django.db import migrations, models


_PATTERNS = (
    (re.compile(r"\bобщество\s+с\s+ограниченной\s+ответственностью\b", re.IGNORECASE), "ООО"),
    (re.compile(r"\bиндивидуальный\s+предприниматель\b", re.IGNORECASE), "ИП"),
)


def _abbreviate(value):
    text = str(value or "").strip()
    if not text:
        return None
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def backfill_short_names(apps, schema_editor):
    Agency = apps.get_model("sku", "Agency")
    for agency in Agency.objects.all().only("id", "agn_name"):
        short_name = _abbreviate(getattr(agency, "agn_name", ""))
        Agency.objects.filter(pk=agency.pk).update(short_name=short_name)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("sku", "0008_agency_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="agency",
            name="short_name",
            field=models.CharField(blank=True, max_length=255, null=True, verbose_name="Сокращенное название"),
        ),
        migrations.RunPython(backfill_short_names, noop_reverse),
    ]
