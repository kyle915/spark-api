"""
Comprehensive tests for GraphQL user registration mutations.

This module tests:
- AmbassadorsCustomRegister.register
- SparkCustomRegister.register
- ClientsCustomRegister.register

Test scenarios include:
- Successful registrations
- Password validation
- Email uniqueness
- Role validation
- Tenant validation (for clients)
- Response structure validation
"""
import pytest
import asyncio
# Ensure strawberry_django is imported before schema imports
import strawberry_django  # noqa: F401
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from tenants.models import Role, Tenant, TenantedUser
from tenants.tests.base import BaseGraphQLTestCase
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestAmbassadorsRegistration(BaseGraphQLTestCase):
    """Tests for AmbassadorsCustomRegister.register mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test.

        The 'db' fixture ensures database access is available.
        """
        # Import schema here to ensure strawberry_django is loaded first
        from config.schema_ambassador import schema_ambassador
        # Create roles in the database transaction
        self.roles = self.setup_default_roles()
        self.schema = schema_ambassador
        self.endpoint_path = "/api/v1/graphql/ambassadors"

    async def _execute_mutation(self, mutation, variables, endpoint_path="/api/v1/graphql/ambassadors"):
        """Helper method to execute GraphQL mutations.

        Args:
            mutation: GraphQL mutation string
            variables: Variables dictionary
            endpoint_path: The actual endpoint path being tested (default: ambassadors)
        """
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        factory = RequestFactory()
        wsgi_request = factory.post(endpoint_path)
        wsgi_request.user = AnonymousUser()

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
                # Create a scope dict that the middleware expects
                # The middleware expects USER_OR_ERROR_KEY with a UserOrError-like object
                self.scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    USER_OR_ERROR_KEY: MockUserOrError(wsgi_request.user),
                }

        mock_request = MockASGIRequest(wsgi_request, endpoint_path)

        # Use execute (async) since mutations are async
        # According to Strawberry docs: https://strawberry.rocks/docs/operations/testing
        result = await self.schema.execute(
            mutation,
            variable_values=variables,
            context_value={"request": mock_request},
        )
        return result

    @pytest.mark.asyncio
    async def test_register_ambassador_success(self):
        """Test successful ambassador registration."""
        # Roles are created by the setup fixture
        # The transaction=True marker ensures proper transaction handling
        mutation = """
        mutation RegisterAmbassador($input: BaseRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "John",
                "email": "ambassador@test.com",
                "password1": "testpass123",
                "password2": "testpass123",
                "clientMutationId": "test-123"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None, f"Result data is None. Errors: {result.errors}"
        assert result.data["register"][
            "success"] is True, f"Registration failed: {result.data['register'].get('message', 'Unknown error')}"
        assert "successfully" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is not None
        assert result.data["register"]["clientMutationId"] == "test-123"

        # Verify user was created
        user = await sync_to_async(User.objects.get)(email="ambassador@test.com")
        assert user.first_name == "John"
        assert user.role.id == ROLE_ID.Ambassadors
        assert user.is_active is True
        assert await sync_to_async(user.check_password)("testpass123")

    @pytest.mark.asyncio
    async def test_register_ambassador_password_mismatch(self):
        """Test ambassador registration with mismatched passwords."""
        mutation = """
        mutation RegisterAmbassador($input: BaseRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """
        variables = {
            "input": {
                "firstName": "John",
                "email": "ambassador2@test.com",
                "password1": "testpass123",
                "password2": "differentpass",
                "clientMutationId": "test-123"
            }
        }
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)
        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "password" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None
        exists = await sync_to_async(User.objects.filter(email="ambassador2@test.com").exists)()
        assert not exists

    @pytest.mark.asyncio
    async def test_register_ambassador_duplicate_email(self):
        """Test ambassador registration with duplicate email."""
        # Create existing user using sync_to_async wrapper
        @sync_to_async
        def create_existing_user():
            return self.create_user(
                username="existing@test.com",
                email="existing@test.com",
                role=self.roles['ambassador']
            )

        await create_existing_user()

        mutation = """
        mutation RegisterAmbassador($input: BaseRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """
        variables = {
            "input": {
                "firstName": "New User",
                "email": "existing@test.com",
                "password1": "testpass123",
                "password2": "testpass123",
            }
        }
        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)
        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "email" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None
        count = await sync_to_async(User.objects.filter(email="existing@test.com").count)()
        assert count == 1


@pytest.mark.django_db(transaction=True)
class TestSparkRegistration(BaseGraphQLTestCase):
    """Tests for SparkCustomRegister.register mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    async def _execute_mutation(self, mutation, variables, endpoint_path="/api/v1/graphql/spark"):
        """Helper method to execute GraphQL mutations."""
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        factory = RequestFactory()
        wsgi_request = factory.post(endpoint_path)
        wsgi_request.user = AnonymousUser()

        class MockUserOrError:
            def __init__(self, user):
                self.user = user
                self.errors = None

        class MockASGIRequest:
            def __init__(self, wsgi_request, path):
                self.wsgi_request = wsgi_request
                self.user = wsgi_request.user
                self.scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    USER_OR_ERROR_KEY: MockUserOrError(wsgi_request.user),
                }

        mock_request = MockASGIRequest(wsgi_request, endpoint_path)
        result = await self.schema.execute(
            mutation,
            variable_values=variables,
            context_value={"request": mock_request},
        )
        return result

    @pytest.mark.asyncio
    async def test_register_spark_admin_success(self):
        """Test successful spark admin registration."""
        mutation = """
        mutation RegisterSpark($input: BaseRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "Spark",
                "email": "spark@test.com",
                "password1": "sparkpass123",
                "password2": "sparkpass123",
                "clientMutationId": "spark-123"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is True
        assert "successfully" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is not None
        assert result.data["register"]["clientMutationId"] == "spark-123"

        user = await sync_to_async(User.objects.get)(email="spark@test.com")
        assert user.first_name == "Spark"
        assert user.role.id == ROLE_ID.SparkAdmin
        assert user.is_active is True
        assert await sync_to_async(user.check_password)("sparkpass123")

    @pytest.mark.asyncio
    async def test_register_spark_admin_password_mismatch(self):
        """Test spark admin registration with mismatched passwords."""
        mutation = """
        mutation RegisterSpark($input: BaseRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "Spark",
                "email": "spark2@test.com",
                "password1": "sparkpass123",
                "password2": "wrongpass",
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "password" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None

        exists = await sync_to_async(User.objects.filter(email="spark2@test.com").exists)()
        assert not exists

    @pytest.mark.asyncio
    async def test_register_spark_admin_duplicate_email(self):
        """Test spark admin registration with duplicate email."""
        @sync_to_async
        def create_existing_user():
            return self.create_user(
                username="spark@test.com",
                email="spark@test.com",
                role=self.roles['spark_admin']
            )

        await create_existing_user()

        mutation = """
        mutation RegisterSpark($input: BaseRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "New Spark",
                "email": "spark@test.com",
                "password1": "newpass123",
                "password2": "newpass123",
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "email" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None


@pytest.mark.django_db(transaction=True)
class TestClientsRegistration(BaseGraphQLTestCase):
    """Tests for ClientsCustomRegister.register mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data before each test."""
        from config.schema_client import schema_clients
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    async def _execute_mutation(self, mutation, variables, endpoint_path="/api/v1/graphql/clients"):
        """Helper method to execute GraphQL mutations."""
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        factory = RequestFactory()
        wsgi_request = factory.post(endpoint_path)
        wsgi_request.user = AnonymousUser()

        class MockUserOrError:
            def __init__(self, user):
                self.user = user
                self.errors = None

        class MockASGIRequest:
            def __init__(self, wsgi_request, path):
                self.wsgi_request = wsgi_request
                self.user = wsgi_request.user
                self.scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    USER_OR_ERROR_KEY: MockUserOrError(wsgi_request.user),
                }

        mock_request = MockASGIRequest(wsgi_request, endpoint_path)
        result = await self.schema.execute(
            mutation,
            variable_values=variables,
            context_value={"request": mock_request},
        )
        return result

    @pytest.mark.asyncio
    async def test_register_client_success(self):
        """Test successful client registration with tenant."""
        mutation = """
        mutation RegisterClient($input: ClientRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "Client",
                "email": "client@test.com",
                "password1": "clientpass123",
                "password2": "clientpass123",
                "roleId": "3",
                "tenantId": str(self.tenant.id),
                "clientMutationId": "client-123"
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is True
        assert "successfully" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is not None
        assert result.data["register"]["clientMutationId"] == "client-123"

        user = await sync_to_async(User.objects.get)(email="client@test.com")
        assert user.first_name == "Client"
        assert user.role.id == 3  # Client role
        assert user.is_active is True
        assert await sync_to_async(user.check_password)("clientpass123")

        tenanted_user = await sync_to_async(TenantedUser.objects.get)(user=user, tenant=self.tenant)
        assert tenanted_user.is_active is True

    @pytest.mark.asyncio
    async def test_register_client_password_mismatch(self):
        """Test client registration with mismatched passwords."""
        mutation = """
        mutation RegisterClient($input: ClientRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "Client",
                "email": "client2@test.com",
                "password1": "clientpass123",
                "password2": "differentpass",
                "roleId": "3",
                "tenantId": str(self.tenant.id),
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "password" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None

        exists = await sync_to_async(User.objects.filter(email="client2@test.com").exists)()
        assert not exists

    @pytest.mark.asyncio
    async def test_register_client_duplicate_email(self):
        """Test client registration with duplicate email."""
        @sync_to_async
        def create_existing_user():
            return self.create_user(
                username="client@test.com",
                email="client@test.com",
                role=self.roles['client']
            )

        await create_existing_user()

        mutation = """
        mutation RegisterClient($input: ClientRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "New Client",
                "email": "client@test.com",
                "password1": "newpass123",
                "password2": "newpass123",
                "roleId": "3",
                "tenantId": str(self.tenant.id),
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "email" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None

    @pytest.mark.asyncio
    async def test_register_client_invalid_role(self):
        """Test client registration with invalid role ID."""
        mutation = """
        mutation RegisterClient($input: ClientRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "Client",
                "email": "client3@test.com",
                "password1": "clientpass123",
                "password2": "clientpass123",
                "roleId": "999",  # Invalid role ID
                "tenantId": str(self.tenant.id),
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "role" in result.data["register"]["message"].lower()
        assert result.data["register"]["activationToken"] is None

        exists = await sync_to_async(User.objects.filter(email="client3@test.com").exists)()
        assert not exists

    @pytest.mark.asyncio
    async def test_register_client_invalid_tenant(self):
        """Test client registration with invalid tenant ID."""
        mutation = """
        mutation RegisterClient($input: ClientRegisterInput!) {
            register(input: $input) {
                success
                message
                activationToken
                clientMutationId
            }
        }
        """

        variables = {
            "input": {
                "firstName": "Client",
                "email": "client4@test.com",
                "password1": "clientpass123",
                "password2": "clientpass123",
                "roleId": "3",
                "tenantId": "999",  # Invalid tenant ID
            }
        }

        result = await self._execute_mutation(
            mutation, variables, self.endpoint_path)

        assert result.data is not None
        assert result.data["register"]["success"] is False
        assert "tenant" in result.data["register"]["message"].lower()


@pytest.mark.django_db(transaction=True)
class TestRegistrationModelCreation(BaseGraphQLTestCase):
    """Tests for direct model creation (user, tenant, role)."""

    def test_create_role(self):
        """Test creating a role directly."""
        role = self.create_role(name="Test Role")

        assert role.id is not None
        assert role.name == "Test Role"
        assert role.slug == "test-role"
        assert role.created_by is not None

    def test_create_role_with_fixed_id(self):
        """Test creating a role with a fixed ID."""
        role = self.create_role(name="Fixed Role", role_id=10)

        assert role.id == 10
        assert role.name == "Fixed Role"

    def test_create_tenant(self):
        """Test creating a tenant directly."""
        tenant = self.create_tenant(name="Test Company")

        assert tenant.id is not None
        assert tenant.name == "Test Company"
        assert tenant.created_by is not None
        assert tenant.uuid is not None

    def test_create_user(self):
        """Test creating a user directly."""
        roles = self.setup_default_roles()
        user = self.create_user(
            username="testuser",
            email="testuser@test.com",
            role=roles['ambassador'],
            password="testpass123"
        )

        assert user.id is not None
        assert user.username == "testuser"
        assert user.email == "testuser@test.com"
        assert user.role == roles['ambassador']
        assert user.check_password("testpass123")
        assert user.is_active is True

    def test_create_tenanted_user(self):
        """Test creating a tenanted user relationship."""
        roles = self.setup_default_roles()
        tenant = self.create_tenant(name="Test Company")
        user = self.create_user(
            username="tenanted",
            email="tenanted@test.com",
            role=roles['client']
        )

        tenanted_user = self.create_tenanted_user(user=user, tenant=tenant)

        assert tenanted_user.id is not None
        assert tenanted_user.user == user
        assert tenanted_user.tenant == tenant
        assert tenanted_user.is_active is True

    def test_setup_default_roles(self):
        """Test setting up default roles."""
        roles = self.setup_default_roles()

        assert 'ambassador' in roles
        assert 'spark_admin' in roles
        assert 'client' in roles

        assert roles['ambassador'].id == ROLE_ID.Ambassadors
        assert roles['spark_admin'].id == ROLE_ID.SparkAdmin
        assert roles['client'].id == 3

        assert roles['ambassador'].name == "Ambassador"
        assert roles['spark_admin'].name == "Spark Admin"
        assert roles['client'].name == "Client"
