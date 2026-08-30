from django.db import migrations, models
import django.db.models.deletion
import uuid6


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0042_checkin_markets_and_sampling_stops'),
        ('events', '0058_request_cases_to_be_shipped'),
        ('tenants', '0043_tenant_checkin_storage_units'),
    ]

    operations = [
        migrations.CreateModel(
            name='PayableMileageClaim',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('uuid', models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ('shift_label', models.CharField(blank=True, default='', max_length=64)),
                ('started_from_storage', models.BooleanField(default=False)),
                ('storage_market', models.CharField(blank=True, default='', max_length=255)),
                ('storage_address', models.CharField(blank=True, default='', max_length=512)),
                ('storage_lat', models.FloatField(blank=True, null=True)),
                ('storage_lng', models.FloatField(blank=True, null=True)),
                ('stops', models.JSONField(blank=True, default=list)),
                ('payable_miles', models.DecimalField(decimal_places=2, default=0, max_digits=8)),
                ('route', models.JSONField(blank=True, null=True)),
                ('route_source', models.CharField(blank=True, default='', max_length=16)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('ambassador', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payable_mileage_claims', to='ambassadors.ambassador')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payable_mileage_claims', to='events.event')),
                ('tenant', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='payable_mileage_claims', to='tenants.tenant')),
            ],
        ),
        migrations.AddConstraint(
            model_name='payablemileageclaim',
            constraint=models.UniqueConstraint(fields=('ambassador', 'event', 'shift_label'), name='uniq_payable_mileage_ba_event_shift'),
        ),
        migrations.AddIndex(
            model_name='payablemileageclaim',
            index=models.Index(fields=['event', '-created_at'], name='amb_paymile_event_idx'),
        ),
        migrations.AddIndex(
            model_name='payablemileageclaim',
            index=models.Index(fields=['ambassador', '-created_at'], name='amb_paymile_ba_idx'),
        ),
    ]
