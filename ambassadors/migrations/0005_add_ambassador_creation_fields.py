# Generated manually for ambassador creation and invitation system

import django.db.models.deletion
import uuid6
from django.conf import settings
from django.db import migrations, models


def set_existing_ambassadors_active(apps, schema_editor):
    """Set all existing ambassadors to is_active=True since they were already active."""
    Ambassador = apps.get_model('ambassadors', 'Ambassador')
    Ambassador.objects.all().update(is_active=True)


def reverse_set_existing_ambassadors_active(apps, schema_editor):
    """Reverse migration - set all ambassadors to is_active=False."""
    Ambassador = apps.get_model('ambassadors', 'Ambassador')
    Ambassador.objects.all().update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('ambassadors', '0004_remove_ambassador_location_ambassador_address_and_more'),
        ('tenants', '0005_tenant_image_user_image'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Add is_active field to Ambassador
        migrations.AddField(
            model_name='ambassador',
            name='is_active',
            field=models.BooleanField(default=False),
        ),
        # Set existing ambassadors to active
        migrations.RunPython(
            set_existing_ambassadors_active,
            reverse_set_existing_ambassadors_active,
        ),
        # Create AmbassadorInvitation model
        migrations.CreateModel(
            name='AmbassadorInvitation',
            fields=[
                ('id', models.BigAutoField(primary_key=True, serialize=False)),
                ('uuid', models.UUIDField(default=uuid6.uuid7, editable=False, unique=True)),
                ('email', models.EmailField(max_length=254)),
                ('token', models.CharField(max_length=255, unique=True)),
                ('expires_at', models.DateTimeField()),
                ('is_used', models.BooleanField(default=False)),
                ('used_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('ambassador', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='invitation', to='ambassadors.ambassador')),
                ('created_by', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='ambassador_invitations_created_by', to=settings.AUTH_USER_MODEL)),
                ('invited_by', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='ambassador_invitations_sent', to=settings.AUTH_USER_MODEL)),
                ('tenant', models.ForeignKey(on_delete=django.db.models.deletion.RESTRICT, related_name='ambassador_invitations', to='tenants.tenant')),
                ('updated_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.RESTRICT, related_name='ambassador_invitations_updated_by', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        # Add indexes to Ambassador
        migrations.AddIndex(
            model_name='ambassador',
            index=models.Index(fields=['is_active'], name='ambassadors_is_acti_idx'),
        ),
        migrations.AddIndex(
            model_name='ambassador',
            index=models.Index(fields=['user', 'is_active'], name='ambassadors_user_is_a_idx'),
        ),
        # Add indexes to AmbassadorInvitation
        migrations.AddIndex(
            model_name='ambassadorinvitation',
            index=models.Index(fields=['email', 'is_used'], name='ambassadors_email_is_u_idx'),
        ),
        migrations.AddIndex(
            model_name='ambassadorinvitation',
            index=models.Index(fields=['email', 'is_used', 'expires_at'], name='ambassadors_email_is_e_idx'),
        ),
        migrations.AddIndex(
            model_name='ambassadorinvitation',
            index=models.Index(fields=['token'], name='ambassadors_token_idx'),
        ),
        migrations.AddIndex(
            model_name='ambassadorinvitation',
            index=models.Index(fields=['expires_at'], name='ambassadors_expires_idx'),
        ),
    ]

