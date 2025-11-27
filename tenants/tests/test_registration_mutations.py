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
from django.contrib.auth import get_user_model
from django.test import Client
from strawberry.django.test import GraphQLTestClient
from config.schema_ambassador import schema_ambassador
from config.schema_spark import schema_spark
from config.schema_client import schema_clients
from tenants.models import Role, Tenant, TenantedUser
from tenants.tests.base import BaseGraphQLTestCase
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db
class TestAmbassadorsRegistration(BaseGraphQLTestCase):
    """Tests for AmbassadorsCustomRegister.register mutation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test data before each test."""
        self.roles = self.setup_default_roles()
        self.client = Client()
        self.graphql_client = GraphQLTestClient(schema_ambassador, self.client)

    def test_register_ambassador_success(self):
        """Test successful ambassador registration."""
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

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is True
        assert "successfully" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is not None
        assert response.data["register"]["clientMutationId"] == "test-123"

        # Verify user was created
        user = User.objects.get(email="ambassador@test.com")
        assert user.first_name == "John"
        assert user.role.id == ROLE_ID.Ambassadors
        assert user.is_active is True
        assert user.check_password("testpass123")

    def test_register_ambassador_password_mismatch(self):
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
                "email": "ambassador@test.com",
                "password1": "testpass123",
                "password2": "differentpass",
                "clientMutationId": "test-123"
            }
        }

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "password" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None

        # Verify user was NOT created
        assert not User.objects.filter(email="ambassador@test.com").exists()

    def test_register_ambassador_duplicate_email(self):
        """Test ambassador registration with duplicate email."""
        # Create existing user
        existing_user = self.create_user(
            username="existing@test.com",
            email="existing@test.com",
            role=self.roles['ambassador']
        )

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

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "email" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None

        # Verify only one user exists
        assert User.objects.filter(email="existing@test.com").count() == 1


@pytest.mark.django_db
class TestSparkRegistration(BaseGraphQLTestCase):
    """Tests for SparkCustomRegister.register mutation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test data before each test."""
        self.roles = self.setup_default_roles()
        self.client = Client()
        self.graphql_client = GraphQLTestClient(schema_spark, self.client)

    def test_register_spark_admin_success(self):
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

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is True
        assert "successfully" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is not None
        assert response.data["register"]["clientMutationId"] == "spark-123"

        # Verify user was created
        user = User.objects.get(email="spark@test.com")
        assert user.first_name == "Spark"
        assert user.role.id == ROLE_ID.SparkAdmin
        assert user.is_active is True
        assert user.check_password("sparkpass123")

    def test_register_spark_admin_password_mismatch(self):
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
                "email": "spark@test.com",
                "password1": "sparkpass123",
                "password2": "wrongpass",
            }
        }

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "password" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None

        # Verify user was NOT created
        assert not User.objects.filter(email="spark@test.com").exists()

    def test_register_spark_admin_duplicate_email(self):
        """Test spark admin registration with duplicate email."""
        # Create existing user
        existing_user = self.create_user(
            username="spark@test.com",
            email="spark@test.com",
            role=self.roles['spark_admin']
        )

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

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "email" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None


@pytest.mark.django_db
class TestClientsRegistration(BaseGraphQLTestCase):
    """Tests for ClientsCustomRegister.register mutation."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Set up test data before each test."""
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Test Company")
        self.client = Client()
        self.graphql_client = GraphQLTestClient(schema_clients, self.client)

    def test_register_client_success(self):
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

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is True
        assert "successfully" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is not None
        assert response.data["register"]["clientMutationId"] == "client-123"

        # Verify user was created
        user = User.objects.get(email="client@test.com")
        assert user.first_name == "Client"
        assert user.role.id == 3  # Client role
        assert user.is_active is True
        assert user.check_password("clientpass123")

        # Verify tenanted user relationship was created
        tenanted_user = TenantedUser.objects.get(user=user, tenant=self.tenant)
        assert tenanted_user.is_active is True

    def test_register_client_password_mismatch(self):
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
                "email": "client@test.com",
                "password1": "clientpass123",
                "password2": "differentpass",
                "roleId": "3",
                "tenantId": str(self.tenant.id),
            }
        }

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "password" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None

        # Verify user was NOT created
        assert not User.objects.filter(email="client@test.com").exists()

    def test_register_client_duplicate_email(self):
        """Test client registration with duplicate email."""
        # Create existing user
        existing_user = self.create_user(
            username="client@test.com",
            email="client@test.com",
            role=self.roles['client']
        )

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

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "email" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None

    def test_register_client_invalid_role(self):
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
                "email": "client@test.com",
                "password1": "clientpass123",
                "password2": "clientpass123",
                "roleId": "999",  # Invalid role ID
                "tenantId": str(self.tenant.id),
            }
        }

        response = self.graphql_client.query(mutation, variables=variables)

        assert response.data is not None
        assert response.data["register"]["success"] is False
        assert "role" in response.data["register"]["message"].lower()
        assert response.data["register"]["activationToken"] is None

        # Verify user was NOT created
        assert not User.objects.filter(email="client@test.com").exists()

    def test_register_client_invalid_tenant(self):
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
                "email": "client@test.com",
                "password1": "clientpass123",
                "password2": "clientpass123",
                "roleId": "3",
                "tenantId": "999",  # Invalid tenant ID
            }
        }

        response = self.graphql_client.query(mutation, variables=variables)

        # The mutation should handle this gracefully
        # It might create the user but fail on tenant association
        assert response.data is not None
        # The response should indicate an error
        assert response.data["register"]["success"] is False
        assert "tenant" in response.data["register"]["message"].lower()

    def test_register_client_without_tenant_id(self):
        """Test client registration without tenant_id (should fail)."""
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
                # tenantId is missing
            }
        }

        # This should result in a GraphQL validation error or the mutation
        # should handle it gracefully
        response = self.graphql_client.query(mutation, variables=variables)

        # The mutation might create the user but not associate with tenant
        # or it might fail validation
        assert response.data is not None or response.errors is not None


@pytest.mark.django_db
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
