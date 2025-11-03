"""
Django management command to create a tenant and default roles.

Usage:
    python manage.py create_tenant_and_roles
    python manage.py create_tenant_and_roles --tenant-name "My Company"

This command will:
1. Create or find an existing user to use as the creator
2. Create a tenant with the specified name (default: "Default Tenant")
3. Create two roles with fixed IDs:
   - ID 1: "Ambassador"
   - ID 2: "Spark Admin"
"""

from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from tenants.models import Tenant, Role

User = get_user_model()


class Command(BaseCommand):
    help = 'Creates a tenant and two default roles (Ambassador and Spark Admin)'

    def add_arguments(self, parser):
        parser.add_argument(
            '--tenant-name',
            type=str,
            default='Default Tenant',
            help='Name of the tenant to create',
        )

    def handle(self, *args, **options):
        tenant_name = options['tenant_name']

        # Get or create a system user for tenant creation
        # Try to get an existing superuser or create a system user
        system_user = None
        try:
            system_user = User.objects.filter(is_superuser=True).first()
            if not system_user:
                # Try to get any existing user
                system_user = User.objects.first()
        except Exception:
            pass

        # If no user exists, create a system user
        if not system_user:
            self.stdout.write(
                self.style.WARNING(
                    'No existing user found. Creating roles and system user...'
                )
            )
            try:
                # First, create a temporary System role (we'll update it later with the user)
                # Or use one of the roles we're about to create
                temp_role, _ = Role.objects.get_or_create(
                    name='System',
                )

                # Now create the system user with the role
                system_user = User.objects.create_user(
                    username='system',
                    email='admin@spark.local',
                    first_name='System',
                    role=temp_role,
                    is_superuser=True,
                    is_staff=True,
                    is_active=True,
                )

                # Update the role's created_by if it wasn't set
                if not temp_role.created_by:
                    temp_role.created_by = system_user
                    temp_role.save()

                self.stdout.write(
                    self.style.SUCCESS(
                        f'✓ Created system user: {system_user.username}'
                    )
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(
                        f'Failed to create system user: {e}'
                    )
                )
                # If we can't create a user, we can't proceed since Tenant requires created_by
                return

        # Create or get the tenant
        tenant, created = Tenant.objects.get_or_create(
            name=tenant_name,
            defaults={
                'created_by': system_user,
            }
        )

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f'✓ Created tenant: {tenant.name} (ID: {tenant.id})'
                )
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    f'→ Tenant already exists: {tenant.name} (ID: {tenant.id})'
                )
            )

        # Create roles with fixed IDs (matching the migration)
        roles_created = []
        roles_updated = []

        # Role 1: Ambassador
        ambassador_role, ambassador_created = Role.objects.update_or_create(
            pk=1,
            defaults={
                'name': 'Ambassador',
                'created_by': system_user,
            }
        )
        if ambassador_created:
            roles_created.append('Ambassador')
        else:
            roles_updated.append('Ambassador')
        self.stdout.write(
            self.style.SUCCESS(
                f'✓ {"Created" if ambassador_created else "Updated"} role: {ambassador_role.name} (ID: {ambassador_role.id})'
            )
        )

        # Role 2: Spark Admin
        spark_admin_role, spark_admin_created = Role.objects.update_or_create(
            pk=2,
            defaults={
                'name': 'Spark Admin',
                'created_by': system_user,
            }
        )
        if spark_admin_created:
            roles_created.append('Spark Admin')
        else:
            roles_updated.append('Spark Admin')
        self.stdout.write(
            self.style.SUCCESS(
                f'✓ {"Created" if spark_admin_created else "Updated"} role: {spark_admin_role.name} (ID: {spark_admin_role.id})'
            )
        )

        # Summary
        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS('=' * 50))
        self.stdout.write(self.style.SUCCESS('Summary:'))
        self.stdout.write(self.style.SUCCESS(
            f'  Tenant: {tenant.name} (ID: {tenant.id})'))
        self.stdout.write(self.style.SUCCESS(
            f'  Roles created: {", ".join(roles_created) if roles_created else "None"}'))
        self.stdout.write(self.style.SUCCESS(
            f'  Roles updated: {", ".join(roles_updated) if roles_updated else "None"}'))
        self.stdout.write(self.style.SUCCESS('=' * 50))
