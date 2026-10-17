from django.db import migrations


class Migration(migrations.Migration):
    """Join the photo-hash and recap-AI leaves that both followed 0041."""

    dependencies = [
        ("recaps", "0042_recapaireview"),
        ("recaps", "0042_recap_file_content_sha256"),
    ]

    operations = []
