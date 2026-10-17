from django.db import migrations


class Migration(migrations.Migration):
    """Merge the parallel recaps leaves (photo-sha, AI review, comment thread)."""

    dependencies = [
        ("recaps", "0042_recap_file_content_sha256"),
        ("recaps", "0042_recapaireview"),
        ("recaps", "0043_recapcomment"),
    ]

    operations = []
