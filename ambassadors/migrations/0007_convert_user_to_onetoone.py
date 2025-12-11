# Generated manually for converting Ambassador.user to OneToOneField

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count


def handle_duplicate_ambassadors(apps, schema_editor):
    """
    Handle duplicate Ambassador records for the same user.
    
    Strategy:
    - If any ambassador has is_active=True, keep that one
    - Otherwise, keep the most recently created (by created_at)
    - Delete all other duplicate ambassadors
    """
    Ambassador = apps.get_model('ambassadors', 'Ambassador')
    
    # Find all users with multiple ambassadors
    duplicate_users = (
        Ambassador.objects.values('user_id')
        .annotate(count=Count('id'))
        .filter(count__gt=1)
    )
    
    duplicates_handled = 0
    ambassadors_deleted = 0
    
    for user_data in duplicate_users:
        user_id = user_data['user_id']
        ambassadors = Ambassador.objects.filter(user_id=user_id).order_by(
            '-is_active',  # Active ones first
            '-created_at'   # Then most recent
        )
        
        # Keep the first one (active if exists, otherwise most recent)
        ambassador_to_keep = ambassadors.first()
        duplicates = ambassadors[1:]  # All others are duplicates
        
        # Log and delete duplicates
        for duplicate in duplicates:
            duplicates_handled += 1
            ambassadors_deleted += 1
            duplicate.delete()
        
        if duplicates_handled > 0:
            print(
                f"User {user_id}: Kept ambassador {ambassador_to_keep.id} "
                f"(is_active={ambassador_to_keep.is_active}), "
                f"deleted {len(duplicates)} duplicate(s)"
            )
    
    if duplicates_handled > 0:
        print(f"\nTotal: Handled {duplicates_handled} user(s) with duplicates, "
              f"deleted {ambassadors_deleted} ambassador record(s)")
    else:
        print("No duplicate ambassadors found. All users have at most one ambassador.")


def reverse_handle_duplicates(apps, schema_editor):
    """
    Reverse migration - cannot restore deleted duplicates.
    This is a no-op as we cannot recover deleted data.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0006_rename_ambassadors_is_acti_idx_ambassadors_is_acti_b7cef8_idx_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Step 1: Handle duplicate ambassadors before adding unique constraint
        migrations.RunPython(
            handle_duplicate_ambassadors,
            reverse_handle_duplicates,
        ),
        # Step 2: Change ForeignKey to OneToOneField
        # This will automatically add unique=True constraint
        migrations.AlterField(
            model_name='ambassador',
            name='user',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name='ambassador',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

