import hashlib

from django.db import migrations


def _cell_code(*, FbsStorageCell, location, prefix):
    physical_code = str(location.location_code or "").strip()
    if not physical_code:
        physical_code = (
            f"OS-{int(location.row_no or 0)}-{int(location.section_no or 0)}-"
            f"{int(location.tier_no or 0)}-{int(location.cell_no or 0)}"
        )
    candidate = f"{prefix}{physical_code}"
    if len(candidate) > 64:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12].upper()
        candidate = f"{candidate[:51]}-{digest}"
    if FbsStorageCell.objects.filter(cell_code=candidate).exclude(
        location_id=location.id
    ).exists():
        candidate = f"FBS@LOCATION-{int(location.id)}"
    return candidate


def unify_warehouse_fbs_cells(apps, schema_editor):
    WarehouseLocation = apps.get_model("sklad", "WarehouseLocation")
    FbsStorageCell = apps.get_model("fbs", "FbsStorageCell")
    FbsRack = apps.get_model("fbs", "FbsRack")
    FbsRackCell = apps.get_model("fbs", "FbsRackCell")
    FbsPallet = apps.get_model("fbs", "FbsPallet")

    os_locations = WarehouseLocation.objects.filter(
        zone_code__iexact="OS",
        row_no__gt=0,
        section_no__gt=0,
        tier_no__gt=0,
        cell_no__gt=0,
    ).order_by("id")
    for location in os_locations.iterator():
        cell = FbsStorageCell.objects.filter(location_id=location.id).first()
        if cell is None:
            FbsStorageCell.objects.create(
                cell_code=_cell_code(
                    FbsStorageCell=FbsStorageCell,
                    location=location,
                    prefix="FBS@",
                ),
                location_id=location.id,
                purpose="flex",
                client_cluster=0,
                is_active=bool(location.is_active),
            )
        elif cell.is_active != bool(location.is_active):
            cell.is_active = bool(location.is_active)
            cell.save(update_fields=["is_active", "updated_at"])

    pr_locations = WarehouseLocation.objects.filter(
        zone_code__iexact="PR",
        is_topology_visible=False,
        is_fbs_visible=True,
        is_active=True,
    ).order_by("id")
    for location in pr_locations.iterator():
        storage_cell = FbsStorageCell.objects.filter(location_id=location.id).first()
        if storage_cell is not None and FbsRackCell.objects.filter(
            storage_cell_id=storage_cell.id
        ).exists():
            continue

        if storage_cell is not None and FbsPallet.objects.filter(
            cell_id=storage_cell.id,
            status__in=("planned", "active"),
            is_rack_binding=False,
        ).exists():
            continue
        rack, _created = FbsRack.objects.get_or_create(
            location_id=location.id,
            defaults={"is_active": True},
        )
        if FbsRackCell.objects.filter(rack_id=rack.id).exists():
            continue
        if storage_cell is None:
            storage_cell = FbsStorageCell.objects.create(
                cell_code=_cell_code(
                    FbsStorageCell=FbsStorageCell,
                    location=location,
                    prefix="",
                ),
                location_id=location.id,
                purpose="pick",
                client_cluster=0,
                is_active=True,
            )
        else:
            changed = []
            if not storage_cell.is_active:
                storage_cell.is_active = True
                changed.append("is_active")
            if storage_cell.purpose != "pick":
                storage_cell.purpose = "pick"
                changed.append("purpose")
            if changed:
                storage_cell.save(update_fields=[*changed, "updated_at"])
        FbsRackCell.objects.create(
            rack_id=rack.id,
            storage_cell_id=storage_cell.id,
            position=1,
            is_active=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0053_fbs_pr_racks"),
    ]

    operations = [
        migrations.RunPython(
            unify_warehouse_fbs_cells,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
