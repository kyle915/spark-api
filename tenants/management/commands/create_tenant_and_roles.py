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
from tenants.models import Tenant, Role, TenantedUser
from gqlauth.models import UserStatus

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
                temp_role, _ = Role.objects.update_or_create(
                    pk=4,
                    defaults={
                        'name': 'System',
                        'slug': 'system_user',
                    }
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
                'slug': Role.AMBASSADOR_SLUG,
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
                'slug': Role.SPARK_ADMIN_SLUG,
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

        # Role 3: Client
        client_role, client_created = Role.objects.update_or_create(
            pk=3,
            defaults={
                'name': 'Client',
                'created_by': system_user,
                'slug': Role.CLIENT_SLUG,
            }
        )
        if client_created:
            roles_created.append('Client')
        else:
            roles_updated.append('Client')
        self.stdout.write(
            self.style.SUCCESS(
                f'✓ {"Created" if client_created else "Updated"} role: {client_role.name} (ID: {client_role.id})'
            )
        )

        # Spark Admin User
        spark_admin_user, spark_admin_created = User.objects.update_or_create(
            username='spark-admin',
            email='spark-admin@spark.local',
            first_name='Spark Admin',
            role=spark_admin_role,
            is_superuser=True,
            is_staff=True,
            is_active=True,
        )

        if spark_admin_created:
            spark_admin_user.set_password('password')
            spark_admin_user.save()
        self.stdout.write(
            self.style.SUCCESS(
                f'✓ {"Created" if spark_admin_created else "Updated"} user: {spark_admin_user.username} (ID: {spark_admin_user.id})'
            )
        )

        # Client User
        client_user, client_created = User.objects.update_or_create(
            username='client',
            email='client@spark.local',
            first_name='Client',
            role=client_role,
            is_active=True,
        )

        if client_created:
            client_user.set_password('password')
            client_user.save()
        self.stdout.write(
            self.style.SUCCESS(
                f'✓ {"Created" if client_created else "Updated"} user: {client_user.username} (ID: {client_user.id})'
            )
        )

        # Ambassador User
        ambassador_user, ambassador_created = User.objects.update_or_create(
            username='ambassador',
            email='ambassador@spark.local',
            first_name='Ambassador',
            role=ambassador_role,
            is_active=True,
        )

        if ambassador_created:
            ambassador_user.set_password('password')
            ambassador_user.save()

        # Update existing users to be verified
        for username in ['spark-admin', 'client', 'ambassador']:
            try:
                user = User.objects.get(username=username)
                user_status, created = UserStatus.objects.get_or_create(
                    user=user,
                    defaults={'verified': True, 'archived': False}
                )
                if not created:
                    user_status.verified = True
                    user_status.archived = False
                    user_status.save()

                # also adding users as member of the tenant
                TenantedUser.objects.get_or_create(
                    user=user,
                    tenant=tenant,
                    is_active=True
                )
                self.stdout.write(
                    self.style.SUCCESS(f'✓ Verified user: {user.username}')
                )
            except User.DoesNotExist:
                pass
        self.stdout.write(
            self.style.SUCCESS(
                f'✓ {"Created" if ambassador_created else "Updated"} user: {ambassador_user.username} (ID: {ambassador_user.id})'
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
        self.stdout.write(self.style.SUCCESS(
            f'  Spark Admin User: {spark_admin_user.username} (ID: {spark_admin_user.id})'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'  Client User: {client_user.username} (ID: {client_user.id})'
        ))
        self.stdout.write(self.style.SUCCESS(
            f'  Ambassador User: {ambassador_user.username} (ID: {ambassador_user.id})'
        ))
