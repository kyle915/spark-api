"""
Base test class with helper methods for creating test data.

This class provides utilities for creating users, tenants, roles, and
tenanted user relationships needed for testing GraphQL mutations.
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.utils.text import slugify
from gqlauth.models import UserStatus
from tenants.models import Role, Tenant, TenantedUser
from utils.utils import ROLE_ID

User = get_user_model()


def _sync_role_id_sequence():
    """Point tenants_role's id sequence past the highest existing id.

    0001_initial seeds Ambassador/Spark Admin with EXPLICIT pks, which does
    not advance the sequence — so the first sequence-assigned INSERT gets
    nextval=1 and dies on tenants_role_pkey while those rows exist. Call
    before any Role INSERT that doesn't pass an explicit pk.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('tenants_role', 'id'),"
            " GREATEST(COALESCE((SELECT MAX(id) FROM tenants_role), 0), 1))"
        )


def ensure_role(name: str, *, slug: str | None = None, pk: int | None = None,
                created_by=None) -> Role:
    """Return THE role for ``name``, creating or converging as needed.

    Role fixtures cannot assume a clean table: 0001_initial seeds
    Ambassador/Spark Admin through historical models (explicit pks, custom
    save() skipped, so slug is NULL and the id sequence never advances), any
    transaction=True test's teardown flush deletes those seeds mid-suite,
    and async tests can commit extra rows outside the test transaction.
    ``name`` is the unique constraint fixtures keep colliding on, so this
    looks up by name first, adopts a row squatting on a required ``pk``, and
    keeps the sequence ahead of explicit-pk rows before inserting.
    """
    slug = slug or slugify(name)
    # A different-named row holding our slug (unique) would break both our
    # save and slug-keyed lookups in code under test — release it.
    Role.objects.exclude(name=name).filter(slug=slug).update(slug=None)

    role = Role.objects.filter(name=name).first()
    if role is None and pk is not None:
        # No row with our name, but our required pk may be occupied by a
        # leaked row under another name: converge it so both unique keys
        # (pk and name) end up canonical.
        role = Role.objects.filter(pk=pk).first()
    if role is not None:
        dirty = False
        for field, value in (("name", name), ("slug", slug)):
            if getattr(role, field) != value:
                setattr(role, field, value)
                dirty = True
        if created_by is not None and role.created_by_id is None:
            role.created_by = created_by
            dirty = True
        if dirty:
            role.save()
        return role

    if pk is not None:
        return Role.objects.create(
            pk=pk, name=name, slug=slug, created_by=created_by)
    _sync_role_id_sequence()
    return Role.objects.create(name=name, slug=slug, created_by=created_by)


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
                temp_role = ensure_role('System')

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

        return ensure_role(
            name,
            slug=kwargs.pop('slug', None),
            pk=role_id,
            created_by=kwargs.pop('created_by', system_user),
        )

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

        return {
            'ambassador': ensure_role(
                'Ambassador', pk=ROLE_ID.Ambassadors, created_by=system_user),
            'spark_admin': ensure_role(
                'Spark Admin', pk=ROLE_ID.SparkAdmin, created_by=system_user),
            'client': ensure_role(
                'Client', pk=ROLE_ID.Client, created_by=system_user),
        }

    async def _execute_mutation(self, mutation, variables, endpoint_path=None, user=None):
        """
        Helper method to execute GraphQL mutations.

        This method creates a mock ASGI request and executes the GraphQL mutation
        using the schema set by the test class.

        Args:
            mutation: GraphQL mutation string
            variables: Variables dictionary
            endpoint_path: The actual endpoint path being tested (optional,
                          defaults to self.endpoint_path if set)
            user: Optional user to simulate authentication

        Returns:
            ExecutionResult: The result from schema.execute()
        """
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        # Use endpoint_path from parameter or fall back to instance attribute
        path = endpoint_path or getattr(
            self, 'endpoint_path', '/api/v1/graphql')

        factory = RequestFactory()
        wsgi_request = factory.post(path)
        wsgi_request.user = user or AnonymousUser()

        # Create a mock ASGI request object that JwtSchema expects
        # JwtSchema middleware looks for request.scope or request.consumer.scope
        class MockUserOrError:
            """Mock UserOrError object that the middleware expects."""

            def __init__(self, user):
                self.user = user
                self.errors = None

        class MockASGIRequest:
            def __init__(self, wsgi_request, path):
                self.wsgi_request = wsgi_request
                self.user = wsgi_request.user
                self.META = getattr(wsgi_request, "META", {})
                # Create a scope dict that the middleware expects
                # The middleware expects USER_OR_ERROR_KEY with a UserOrError-like object
                self.scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    USER_OR_ERROR_KEY: MockUserOrError(wsgi_request.user),
                }
                # Add UserOrError attribute as some permissions might check it directly
                setattr(self, USER_OR_ERROR_KEY, MockUserOrError(wsgi_request.user))

        mock_request = MockASGIRequest(wsgi_request, path)

        class Context:
            def __init__(self, request):
                self.request = request
            
            def __getitem__(self, key):
                if key == "request":
                    return self.request
                raise KeyError(key)

        # Use execute (async) since mutations are async
        # According to Strawberry docs: https://strawberry.rocks/docs/operations/testing
        result = await self.schema.execute(
            mutation,
            variable_values=variables,
            context_value=Context(mock_request),
        )
        return result
