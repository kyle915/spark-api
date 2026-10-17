from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0041_customrecap_co_activation_key_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecapAiReview",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("recap_id", models.BigIntegerField()),
                ("is_custom", models.BooleanField(default=False)),
                ("feedback_extract", models.JSONField(blank=True, null=True)),
                (
                    "feedback_source_hash",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("extracted_at", models.DateTimeField(blank=True, null=True)),
                ("summary", models.JSONField(blank=True, null=True)),
                ("summarized_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("recap_id", "is_custom"),
                        name="rc_recap_ai_review_uniq",
                    )
                ],
            },
        ),
    ]
