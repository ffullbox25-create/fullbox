import re

from django.db import migrations, models


GS = "\x1d"
LEGACY_IDENTITY_SEPARATOR = "\x1flegacy:"


def _normalize(value):
    text = str(value or "").strip("\r\n\t ")
    if text[:3].lower() == "]d2":
        text = text[3:]
    text = re.sub(r"_x001d_", GS, text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*gs\s*>", GS, text, flags=re.IGNORECASE)
    text = re.sub(r"\{\s*fnc1\s*\}", GS, text, flags=re.IGNORECASE)
    text = re.sub(r"(?:\\u001d|\\x001d|\\x1d)", GS, text, flags=re.IGNORECASE)
    return text.strip("\r\n\t ")


def _identity(value):
    code = _normalize(value)
    if not (
        len(code) >= 19
        and code.startswith("01")
        and code[2:16].isdigit()
        and code[16:18] == "21"
    ):
        return code
    prefix = code[:18]
    tail = code[18:]
    if GS in tail:
        return prefix + tail.split(GS, 1)[0]
    for index in range(0, max(0, len(tail) - 7)):
        if tail[index : index + 2] == "91" and tail[index + 6 : index + 8] in {"92", "93"}:
            return prefix + tail[:index]
    return code


def backfill_identity_keys(apps, schema_editor):
    MarkingCode = apps.get_model("marking", "MarkingCode")
    seen = set()
    batch = []
    for row in MarkingCode.objects.only("id", "code").order_by("id").iterator(chunk_size=2000):
        identity = _identity(row.code)
        if identity and identity in seen:
            # Do not delete historical rows: they may describe the same label
            # placed in different boxes. One row keeps the canonical key so all
            # future inserts are rejected; additional rows receive stable,
            # unique audit-only keys.
            row.identity_key = f"{identity}{LEGACY_IDENTITY_SEPARATOR}{row.id}"
        else:
            row.identity_key = identity
            if identity:
                seen.add(identity)
        batch.append(row)
        if len(batch) >= 2000:
            MarkingCode.objects.bulk_update(batch, ["identity_key"], batch_size=2000)
            batch = []
    if batch:
        MarkingCode.objects.bulk_update(batch, ["identity_key"], batch_size=2000)


def clear_identity_keys(apps, schema_editor):
    MarkingCode = apps.get_model("marking", "MarkingCode")
    MarkingCode.objects.update(identity_key="")


class Migration(migrations.Migration):
    dependencies = [
        ("marking", "0005_markingcode_print_job_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="markingcode",
            name="identity_key",
            field=models.TextField(blank=True, default="", editable=False, verbose_name="Идентификатор единицы ЧЗ"),
        ),
        migrations.RunPython(backfill_identity_keys, clear_identity_keys),
        migrations.AddConstraint(
            model_name="markingcode",
            constraint=models.UniqueConstraint(
                condition=~models.Q(identity_key=""),
                fields=("identity_key",),
                name="uniq_marking_code_identity",
            ),
        ),
    ]
