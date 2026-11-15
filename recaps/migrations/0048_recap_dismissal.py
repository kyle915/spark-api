# Missing Recaps: a dismissal record ("no recap is owed here"), restorable.

import django.db.models.deletion
import uuid6
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0047_shiftsms_attempts'),
        ('events', '0071_account_list_entry'),
        ('recaps', '0047_retailer_proposal'),
        ('tenants', '0061_tenant_recap_skip_emails'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RecapDismissal',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('uuid', models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ('reason', models.CharField(choices=[('did_not_work', 'Did not work the shift'), ('filed_elsewhere', 'Recap filed elsewhere'), ('duplicate_or_test', 'Duplicate or test shift'), ('not_needed', 'Not needed for this event'), ('other', 'Other')], max_length=24)),
                ('note', models.CharField(blank=True, default='', max_length=280)),
                ('dismissed_at', models.DateTimeField(auto_now_add=True)),
                ('restored_at', models.DateTimeField(blank=True, null=True)),
                ('ambassador', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='recap_dismissals', to='ambassadors.ambassador')),
                ('dismissed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recap_dismissals_made', to=settings.AUTH_USER_MODEL)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recap_dismissals', to='events.event')),
                ('restored_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recap_dismissals_restored', to=settings.AUTH_USER_MODEL)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='recap_dismissals', to='tenants.tenant')),
            ],
            options={
                'indexes': [models.Index(condition=models.Q(('restored_at__isnull', True)), fields=['tenant', '-dismissed_at'], name='recap_dismissal_tenant_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('ambassador__isnull', False), ('restored_at__isnull', True)), fields=('event', 'ambassador'), name='recap_dismissal_active_pair_uniq'), models.UniqueConstraint(condition=models.Q(('ambassador__isnull', True), ('restored_at__isnull', True)), fields=('event',), name='recap_dismissal_active_event_uniq')],
            },
        ),
    ]
