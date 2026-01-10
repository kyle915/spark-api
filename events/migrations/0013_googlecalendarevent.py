# Generated manually for Google Calendar Event mapping

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import uuid6


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0012_product_image'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='GoogleCalendarEvent',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('uuid', models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ('google_event_id', models.CharField(max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='google_calendar_events', to='events.event')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='google_calendar_event_mappings', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'unique_together': {('event', 'user')},
            },
        ),
        migrations.AddIndex(
            model_name='googlecalendarevent',
            index=models.Index(fields=['event', 'user'], name='events_goog_event_i_2a1f8b_idx'),
        ),
    ]

