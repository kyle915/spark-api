from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("ambassadors", "0043_payablemileageclaim"),
        ("events", "0058_request_cases_to_be_shipped"),
        ("recaps", "0041_customrecap_co_activation_key_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalkupRecapDraft",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("scope", models.CharField(default="shift", max_length=16)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "ambassador",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="walkup_recap_drafts",
                        to="ambassadors.ambassador",
                    ),
                ),
                (
                    "custom_recap",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="walkup_drafts",
                        to="recaps.customrecap",
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="walkup_recap_drafts",
                        to="events.event",
                    ),
                ),
                (
                    "legacy_recap",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="walkup_drafts",
                        to="recaps.recap",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="walkuprecapdraft",
            constraint=models.UniqueConstraint(
                fields=("event", "ambassador", "scope"),
                name="uniq_walkup_recap_draft_scope",
            ),
        ),
    ]
