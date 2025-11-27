"""
Base test class with helper methods for creating test data.

This class provides utilities for creating users, tenants, roles, and
tenanted user relationships needed for testing GraphQL mutations.
"""
from django.contrib.auth import get_user_model
from gqlauth.models import UserStatus
from tenants.models import Role, Tenant, TenantedUser
from utils.utils import ROLE_ID

User = get_user_model()


class BaseGraphQLTestCase:
    """
    Base test class providing helper methods for creating test data.

    This class handles the circular dependency of needing a user to create
    tenants/roles by providing a system user creation method.
    """

    _system_user = None

    def get_system_user(self):
        """
        Get or create a system user for tenant/role creation.

        This user is used as the created_by field for tenants and roles,
        which is required by the models.

        Returns:
            User: A system user instance
        """
        if self._system_user is None:
            # Try to get an existing system user
            try:
                self._system_user = User.objects.get(username='system')
            except User.DoesNotExist:
                # Create a temporary role for the system user
                temp_role, _ = Role.objects.get_or_create(
                    name='System',
                    defaults={'slug': 'system'}
                )

                # Create the system user
                self._system_user = User.objects.create_user(
                    username='system',
                    email='system@spark.local',
                    first_name='System',
                    role=temp_role,
                    is_superuser=True,
                    is_staff=True,
                    is_active=True,
                )

                # Update the role's created_by if it wasn't set
                if not temp_role.created_by:
                    temp_role.created_by = self._system_user
                    temp_role.save()

        return self._system_user

    def create_role(self, name: str, role_id: int | None = None, **kwargs):
        """
        Create a Role instance.

        Args:
            name: Name of the role (e.g., 'Ambassador', 'Spark Admin', 'Client')
            role_id: Optional ID to use for the role (for fixed IDs)
            **kwargs: Additional fields to set on the role

        Returns:
            Role: The created role instance
        """
        system_user = self.get_system_user()

        defaults = {
            'created_by': system_user,
            **kwargs
        }

        if role_id:
            role, _ = Role.objects.update_or_create(
                pk=role_id,
                defaults={'name': name, **defaults}
            )
        else:
            role = Role.objects.create(name=name, **defaults)

        return role

    def create_tenant(self, name: str = "Test Tenant", **kwargs):
        """
        Create a Tenant instance.

        Args:
            name: Name of the tenant
            **kwargs: Additional fields to set on the tenant

        Returns:
            Tenant: The created tenant instance
        """
        system_user = self.get_system_user()

        tenant = Tenant.objects.create(
            name=name,
            created_by=system_user,
            **kwargs
        )

        return tenant

    def create_user(
        self,
        username: str,
        email: str,
        role: Role,
        password: str = "password",
        **kwargs
    ):
        """
        Create a User instance.

        Args:
            username: Username for the user
            email: Email for the user
            role: Role instance to assign to the user
            password: Password for the user (default: "password")
            **kwargs: Additional fields to set on the user

        Returns:
            User: The created user instance
        """
        user = User.objects.create_user(
            username=username,
            email=email,
            role=role,
            password=password,
            **kwargs
        )

        # Create UserStatus for the user (required by gqlauth)
        UserStatus.objects.get_or_create(
            user=user,
            defaults={'verified': True, 'archived': False}
        )

        return user

    def create_tenanted_user(self, user: User, tenant: Tenant, **kwargs):
        """
        Create a TenantedUser relationship.

        Args:
            user: User instance
            tenant: Tenant instance
            **kwargs: Additional fields to set on the tenanted user

        Returns:
            TenantedUser: The created tenanted user instance
        """
        system_user = self.get_system_user()

        tenanted_user = TenantedUser.objects.create(
            user=user,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return tenanted_user

    def setup_default_roles(self):
        """
        Create default roles (Ambassador, Spark Admin, Client) with fixed IDs.

        This method mimics the behavior of the create_tenant_and_roles
        management command.

        Returns:
            dict: Dictionary with role names as keys and Role instances as values
        """
        system_user = self.get_system_user()

        roles = {}

        # Role 1: Ambassador
        roles['ambassador'], _ = Role.objects.update_or_create(
            pk=ROLE_ID.Ambassadors,
            defaults={
                'name': 'Ambassador',
                'created_by': system_user,
            }
        )

        # Role 2: Spark Admin
        roles['spark_admin'], _ = Role.objects.update_or_create(
            pk=ROLE_ID.SparkAdmin,
            defaults={
                'name': 'Spark Admin',
                'created_by': system_user,
            }
        )

        # Role 3: Client
        roles['client'], _ = Role.objects.update_or_create(
            pk=3,
            defaults={
                'name': 'Client',
                'created_by': system_user,
            }
        )

        return roles
