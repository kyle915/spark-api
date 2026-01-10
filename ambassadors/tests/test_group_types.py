"""
Tests for group type mutations and queries.

This module tests:
- create_group_type mutation (client/spark-admin only)
- update_group_type mutation (client/spark-admin only)
- group_types query (authenticated users)
- group_type query (authenticated users)
"""
import pytest
import strawberry_django  # noqa: F401
import base64
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import GroupType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestCreateGroupType(AmbassadorsGraphQLTestCase):
    """Tests for create_group_type mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Group Type Creation Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_gt_{unique_id}@test.com",
            email=f"client_gt_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_gt_{unique_id2}@test.com",
            email=f"spark_gt_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_gt_{unique_id3}@test.com",
            email=f"ambassador_gt_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateGroupType($input: CreateGroupTypeInput!) {
                createGroupType(input: $input) {
                    success
                    message
                    clientMutationId
                    groupType {
                        id
                        name
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_group_type_success_by_client(self):
        """Test successful group type creation by client."""
        variables = {
            "input": {
                "name": "Marketing Team",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createGroupType"]["success"] is True
        assert "created successfully" in result.data["createGroupType"]["message"].lower(
        )
        assert result.data["createGroupType"]["clientMutationId"] == "test-123"
        assert result.data["createGroupType"]["groupType"]["name"] == "Marketing Team"

        # Verify in DB
        @sync_to_async
        def get_group_type():
            return GroupType.objects.select_related('created_by').get(name="Marketing Team")
        group_type = await get_group_type()
        assert group_type.created_by == self.client_user

    @pytest.mark.asyncio
    async def test_create_group_type_success_by_spark_admin(self):
        """Test successful group type creation by spark-admin."""
        variables = {
            "input": {
                "name": "Sales Team",
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createGroupType"]["success"] is True
        assert "created successfully" in result.data["createGroupType"]["message"].lower(
        )
        assert result.data["createGroupType"]["groupType"]["name"] == "Sales Team"

    @pytest.mark.asyncio
    async def test_create_group_type_unauthorized_ambassador(self):
        """Test group type creation by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "name": "Unauthorized Group",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0
        assert "You do not have permission to perform this action. Client or Spark Admin access required." in result.errors[
            0].message

    @pytest.mark.asyncio
    async def test_create_group_type_unauthorized_anonymous(self):
        """Test group type creation by unauthorized user (anonymous)."""
        variables = {
            "input": {
                "name": "Anonymous Group",
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestUpdateGroupType(AmbassadorsGraphQLTestCase):
    """Tests for update_group_type mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Group Type Update Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_update_{unique_id}@test.com",
            email=f"client_update_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_update_{unique_id2}@test.com",
            email=f"spark_update_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_update_{unique_id3}@test.com",
            email=f"ambassador_update_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type = GroupType.objects.create(
            name="Old Group Type Name",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation UpdateGroupType($input: UpdateGroupTypeInput!) {
                updateGroupType(input: $input) {
                    success
                    message
                    clientMutationId
                    groupType {
                        id
                        name
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_update_group_type_success_by_client(self):
        """Test successful group type update by client."""
        variables = {
            "input": {
                "id": str(self.group_type.id),
                "name": "New Group Type Name",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateGroupType"]["success"] is True
        assert "updated successfully" in result.data["updateGroupType"]["message"].lower(
        )
        assert result.data["updateGroupType"]["groupType"]["name"] == "New Group Type Name"

        # Verify in DB
        @sync_to_async
        def get_group_type():
            return GroupType.objects.select_related('updated_by').get(pk=self.group_type.id)
        group_type = await get_group_type()
        assert group_type.name == "New Group Type Name"
        assert group_type.updated_by == self.client_user

    @pytest.mark.asyncio
    async def test_update_group_type_success_by_spark_admin(self):
        """Test successful group type update by spark-admin."""
        variables = {
            "input": {
                "id": str(self.group_type.id),
                "name": "Updated By Spark Admin",
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateGroupType"]["success"] is True
        assert result.data["updateGroupType"]["groupType"]["name"] == "Updated By Spark Admin"

    @pytest.mark.asyncio
    async def test_update_group_type_not_found(self):
        """Test update of non-existent group type."""
        variables = {
            "input": {
                "id": "999999",
                "name": "New Name",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is not None
        assert len(result.errors) > 0
        assert "GroupType matching query does not exist" in result.errors[0].message

    @pytest.mark.asyncio
    async def test_update_group_type_unauthorized_ambassador(self):
        """Test group type update by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "id": str(self.group_type.id),
                "name": "Unauthorized Update",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_update_group_type_unauthorized_anonymous(self):
        """Test group type update by unauthorized user (anonymous)."""
        variables = {
            "input": {
                "id": str(self.group_type.id),
                "name": "Anonymous Update",
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestGroupTypeQueries(AmbassadorsGraphQLTestCase):
    """Tests for group type queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Group Type Query Tenant")

        unique_id = str(uuid.uuid4())[:8]
        # Queries require IsClientOrSparkAdmin
        self.user = self.create_user(
            username=f"user_query_{unique_id}@test.com",
            email=f"user_query_{unique_id}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.user, self.tenant)

        system_user = self.get_system_user()
        self.group_type1 = GroupType.objects.create(
            name="Marketing",
            created_by=system_user,
            updated_by=system_user,
        )
        self.group_type2 = GroupType.objects.create(
            name="Sales",
            created_by=system_user,
            updated_by=system_user,
        )
        self.group_type3 = GroupType.objects.create(
            name="Support",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_group_types_list_success(self):
        """Test successful group types list query."""
        query = """
            query {
                groupTypes(first: 10) {
                    edges {
                        node {
                            id
                            name
                        }
                    }
                    totalCount
                }
            }
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["groupTypes"]["totalCount"] >= 3
        group_type_names = [edge["node"]["name"]
                            for edge in result.data["groupTypes"]["edges"]]
        assert "Marketing" in group_type_names
        assert "Sales" in group_type_names
        assert "Support" in group_type_names

    @pytest.mark.asyncio
    async def test_group_types_filter_by_search(self):
        """Test group types query with search filter."""
        query = """
            query {
                groupTypes(filters: { search: "Sale" }) {
                    edges {
                        node {
                            name
                        }
                    }
                    totalCount
                }
            }
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["groupTypes"]["totalCount"] >= 1
        group_type_names = [edge["node"]["name"]
                            for edge in result.data["groupTypes"]["edges"]]
        assert "Sales" in group_type_names

    @pytest.mark.asyncio
    async def test_group_type_single_success(self):
        """Test successful single group type query."""
        query = f"""
            query {{
                groupType(groupTypeId: "{self.group_type1.id}") {{
                    id
                    name
                }}
            }}
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        decoded_id = base64.b64decode(
            result.data["groupType"]["id"]).decode("utf-8")
        assert decoded_id == f"GroupType:{self.group_type1.id}"
        assert result.data["groupType"]["name"] == "Marketing"

    @pytest.mark.asyncio
    async def test_group_type_single_not_found(self):
        """Test single group type query with non-existent ID."""
        query = """
            query {
                groupType(groupTypeId: "999999") {
                    id
                    name
                }
            }
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["groupType"] is None

    @pytest.mark.asyncio
    async def test_group_types_unauthorized(self):
        """Test group types query by unauthorized user."""
        query = """
            query {
                groupTypes(first: 10) {
                    edges {
                        node {
                            id
                            name
                        }
                    }
                }
            }
        """

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_query_authenticated(
            query, None, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
