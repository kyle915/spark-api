import uuid6

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0048_checkinprogramchange'),
        ('events', '0001_initial'),
        ('tenants', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='CheckinStoreChange',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('uuid', models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ('from_address', models.TextField(blank=True, default='')),
                ('to_address', models.TextField(blank=True, default='')),
                ('from_store_name', models.CharField(blank=True, default='', max_length=255)),
                ('to_store_name', models.CharField(blank=True, default='', max_length=255)),
                ('store_number', models.CharField(blank=True, default='', max_length=64)),
                ('created_retailer', models.BooleanField(default=False)),
                ('old_event_name', models.CharField(blank=True, default='', max_length=255)),
                ('new_event_name', models.CharField(blank=True, default='', max_length=255)),
                ('gps_latitude', models.FloatField(blank=True, null=True)),
                ('gps_longitude', models.FloatField(blank=True, null=True)),
                ('gps_accuracy_m', models.FloatField(blank=True, null=True)),
                ('place_latitude', models.FloatField(blank=True, null=True)),
                ('place_longitude', models.FloatField(blank=True, null=True)),
                ('distance_km', models.FloatField(blank=True, null=True)),
                ('far_override', models.BooleanField(default=False)),
                ('address_source', models.CharField(blank=True, default='', max_length=16)),
                ('request_key', models.CharField(blank=True, default='', max_length=64)),
                ('detail', models.JSONField(blank=True, default=dict)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('ambassador', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkin_store_changes', to='ambassadors.ambassador')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkin_store_changes', to='events.event')),
                ('from_retailer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='events.retailer')),
                ('to_retailer', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='events.retailer')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='checkin_store_changes', to='tenants.tenant')),
            ],
            options={
                'indexes': [models.Index(fields=['event', 'ambassador', '-created_at'], name='ambassadors_event_i_csc_idx')],
            },
        ),
    ]
