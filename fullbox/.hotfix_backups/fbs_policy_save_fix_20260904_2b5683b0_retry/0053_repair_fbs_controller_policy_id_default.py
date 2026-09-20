from django.db import migrations


REPAIR_CONTROLLER_POLICY_ID = r"""
DO $$
DECLARE
    max_policy_id bigint;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'fbs_controller_policy'
          AND column_name = 'id'
          AND is_identity = 'NO'
          AND column_default IS NULL
    ) THEN
        CREATE SEQUENCE IF NOT EXISTS fbs_controller_policy_id_seq AS bigint;

        ALTER SEQUENCE fbs_controller_policy_id_seq
            OWNED BY fbs_controller_policy.id;

        SELECT MAX(id)
        INTO max_policy_id
        FROM fbs_controller_policy;

        IF max_policy_id IS NULL THEN
            PERFORM setval('fbs_controller_policy_id_seq', 1, false);
        ELSE
            PERFORM setval(
                'fbs_controller_policy_id_seq',
                max_policy_id,
                true
            );
        END IF;

        ALTER TABLE fbs_controller_policy
            ALTER COLUMN id
            SET DEFAULT nextval('fbs_controller_policy_id_seq');
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("fbs", "0052_fbs_controller_policy"),
    ]

    operations = [
        migrations.RunSQL(
            sql=REPAIR_CONTROLLER_POLICY_ID,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
