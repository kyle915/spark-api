# Product questions box on the BA web check-in.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0047_shiftsms_attempts'),
        ('events', '0072_retailer_alias'),
        ('tenants', '0065_publish_weekly_summary_drafts'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='ba_product_notes',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.CreateModel(
            name='BaProductQuestion',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('question', models.CharField(max_length=300)),
                ('answer', models.TextField(blank=True, default='')),
                ('outcome', models.CharField(choices=[('answered', 'Answered from notes'), ('not_in_notes', 'Not in the notes'), ('no_material', 'No notes yet'), ('ai_unavailable', 'AI unavailable')], max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('ambassador', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ba_product_questions', to='ambassadors.ambassador')),
                ('event', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ba_product_questions', to='events.event')),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ba_product_questions', to='tenants.tenant')),
            ],
            options={
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['tenant', '-created_at'], name='tenants_bap_tenant__168546_idx')],
            },
        ),
    ]
