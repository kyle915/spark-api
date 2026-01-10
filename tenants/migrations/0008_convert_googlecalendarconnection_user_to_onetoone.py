# Generated manually for converting GoogleCalendarConnection.user to OneToOneField

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import Count


def handle_duplicate_connections(apps, schema_editor):
    """
    Handle duplicate GoogleCalendarConnection records for the same user.
    
    Strategy:
    - If any connection has is_active=True, keep that one
    - Otherwise, keep the most recently created (by created_at)
    - Delete all other duplicate connections
    """
    GoogleCalendarConnection = apps.get_model('tenants', 'GoogleCalendarConnection')
    
    # Find all users with multiple connections
    duplicate_users = (
        GoogleCalendarConnection.objects.values('user_id')
        .annotate(count=Count('id'))
        .filter(count__gt=1)
    )
    
    duplicates_handled = 0
    connections_deleted = 0
    
    for user_data in duplicate_users:
        user_id = user_data['user_id']
        connections = GoogleCalendarConnection.objects.filter(user_id=user_id).order_by(
            '-is_active',  # Active ones first
            '-created_at'   # Then most recent
        )
        
        # Keep the first one (active if exists, otherwise most recent)
        connection_to_keep = connections.first()
        duplicates = connections[1:]  # All others are duplicates
        
        # Log and delete duplicates
        for duplicate in duplicates:
            duplicates_handled += 1
            connections_deleted += 1
            duplicate.delete()
        
        if duplicates_handled > 0:
            print(
                f"User {user_id}: Kept connection {connection_to_keep.id} "
                f"(is_active={connection_to_keep.is_active}), "
                f"deleted {len(duplicates)} duplicate(s)"
            )
    
    if duplicates_handled > 0:
        print(f"\nTotal: Handled {duplicates_handled} user(s) with duplicates, "
              f"deleted {connections_deleted} connection record(s)")
    else:
        print("No duplicate Google Calendar connections found. All users have at most one connection.")


def reverse_handle_duplicates(apps, schema_editor):
    """
    Reverse migration - cannot restore deleted duplicates.
    This is a no-op as we cannot recover deleted data.
    """
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenants', '0007_alter_googlecalendarconnection_refresh_token_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Step 1: Handle duplicate connections before adding unique constraint
        migrations.RunPython(
            handle_duplicate_connections,
            reverse_handle_duplicates,
        ),
        # Step 2: Change ForeignKey to OneToOneField
        # This will automatically add unique=True constraint
        migrations.AlterField(
            model_name='googlecalendarconnection',
            name='user',
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.RESTRICT,
                related_name='google_calendar_connection',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

