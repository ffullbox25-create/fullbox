from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("fbs", "0062_allow_multiple_fbs_pallets_per_shared_place")]
    operations = [migrations.RemoveConstraint(
        model_name="fbsclientmovementrequest",
        name="fbs_mov_box_mode_no_mix",
    )]
