import django.db.models.deletion
import uuid6
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('recaps', '0038_plan_recap_link'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='RecapMetricEdit',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('batch', models.UUIDField(db_index=True, default=uuid6.uuid7)),
                ('field_key', models.CharField(max_length=255)),
                ('field_label', models.CharField(max_length=255)),
                ('old_value', models.TextField(blank=True, null=True)),
                ('new_value', models.TextField(blank=True, null=True)),
                ('reason', models.TextField(blank=True, default='')),
                ('edited_at', models.DateTimeField(auto_now_add=True)),
                ('custom_recap', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='metric_edits', to='recaps.customrecap')),
                ('edited_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recap_metric_edits', to=settings.AUTH_USER_MODEL)),
                ('recap', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='metric_edits', to='recaps.recap')),
            ],
            options={
                'ordering': ['-edited_at', '-id'],
                'constraints': [models.CheckConstraint(condition=models.Q(models.Q(('custom_recap__isnull', True), ('recap__isnull', False)), models.Q(('custom_recap__isnull', False), ('recap__isnull', True)), _connector='OR'), name='rc_metric_edit_one_recap')],
            },
        ),
    ]
