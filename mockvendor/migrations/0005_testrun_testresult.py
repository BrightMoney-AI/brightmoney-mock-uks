"""Persisted test-suite runs + per-case results (dashboard test runner)."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('mockvendor', '0004_calllog_ip_run_id'),
    ]

    operations = [
        migrations.CreateModel(
            name='TestRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('csv_path', models.CharField(max_length=255)),
                ('tag', models.CharField(blank=True, max_length=120)),
                ('case_filter', models.TextField(blank=True)),
                ('mock_base', models.CharField(default='http://127.0.0.1', max_length=255)),
                ('status', models.CharField(choices=[('pending', 'pending'), ('running', 'running'), ('passed', 'passed'), ('failed', 'failed'), ('error', 'error'), ('cancelled', 'cancelled')], db_index=True, default='pending', max_length=16)),
                ('total', models.IntegerField(default=0)),
                ('passed', models.IntegerField(default=0)),
                ('failed', models.IntegerField(default=0)),
                ('skipped', models.IntegerField(default=0)),
                ('error', models.TextField(blank=True)),
                ('log_path', models.CharField(blank=True, max_length=255)),
                ('pid', models.IntegerField(blank=True, null=True)),
                ('created_ip', models.CharField(blank=True, max_length=64)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
            ],
            options={'db_table': 'mockvendor_testrun', 'ordering': ['-id']},
        ),
        migrations.CreateModel(
            name='TestResult',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('case_id', models.CharField(db_index=True, max_length=120)),
                ('passed', models.BooleanField(default=False)),
                ('skipped', models.BooleanField(default=False)),
                ('errors', models.JSONField(blank=True, default=list)),
                ('duration_ms', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('run', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='results', to='mockvendor.testrun')),
            ],
            options={'db_table': 'mockvendor_testresult', 'ordering': ['id']},
        ),
    ]
