# Merge parallel pilot 0003 leaves landed by SPA-29/UTM (#1510) and SPA-32 (#1509).

from django.db import migrations


class Migration(migrations.Migration):
    """Join onboarding marketing attribution + POS/buyer digest graphs."""

    dependencies = [
        ("pilot", "0003_onboarding_marketing_attribution"),
        ("pilot", "0003_pos_feeds_and_buyer_digest"),
    ]

    operations = []
