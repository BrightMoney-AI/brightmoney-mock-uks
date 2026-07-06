"""Add caller-IP + matched-run_id columns to CallLog for parallel-run isolation.

Additive and nullable-safe (both default to ""), so this is a zero-downtime
migration on the shared Postgres. CallLog rows are never deleted (see the model
docstring); this only widens the row.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mockvendor', '0003_scenariobundle'),
    ]

    operations = [
        migrations.AddField(
            model_name='calllog',
            name='request_ip',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.AddField(
            model_name='calllog',
            name='matched_run_id',
            field=models.CharField(blank=True, max_length=120),
        ),
    ]
