"""Document the Recap.submitted_at Python/GraphQL alias.

The persisted column stays ``submited_at`` (typo) so existing writes keep
working. Recap.submitted_at is a property + GraphQL field that reads/writes
that column. No schema rename.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("recaps", "0028_customrecap_data_quality_flags"),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
