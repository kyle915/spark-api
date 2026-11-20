import uuid6

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0047_shiftsms_attempts'),
        ('events', '0001_initial'),
        ('tenants', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='CheckinProgramChange',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('uuid', models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ('from_name', models.CharField(blank=True, default='', max_length=255)),
                ('to_name', models.CharField(blank=True, default='', max_length=255)),
                ('from_template_id', models.IntegerField(blank=True, null=True)),
                ('to_template_id', models.IntegerField(blank=True, null=True)),
                ('old_event_name', models.CharField(blank=True, default='', max_length=255)),
                ('new_event_name', models.CharField(blank=True, default='', max_length=255)),
                ('store_name', models.CharField(blank=True, default='', max_length=255)),
                ('detail', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('ambassador', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkin_program_changes', to='ambassadors.ambassador')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkin_program_changes', to='events.event')),
                ('from_event_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='events.eventtype')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkin_program_changes', to='tenants.tenant')),
                ('to_event_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='events.eventtype')),
            ],
            options={
                'indexes': [models.Index(fields=['event', 'ambassador', '-created_at'], name='ambassadors_event_i_cpc_idx')],
            },
        ),
    ]
