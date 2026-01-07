"""
Tests for ambassador group mutations and queries.

This module tests:
- create_ambassador_group mutation (client/spark-admin only)
- update_ambassador_group mutation (client/spark-admin only)
- delete_ambassador_group mutation (client/spark-admin only)
- ambassador_groups query (client/spark-admin only)
- ambassador_group query (client/spark-admin only)
"""
import pytest
import strawberry_django  # noqa: F401
import base64
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorGroup, GroupType
from ambassadors.tests.base import AmbassadorsGraphQLTestCase

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestCreateAmbassadorGroup(AmbassadorsGraphQLTestCase):
    """Tests for create_ambassador_group mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(
            name="Ambassador Group Creation Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_ag_{unique_id}@test.com",
            email=f"client_ag_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_ag_{unique_id2}@test.com",
            email=f"spark_ag_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_ag_{unique_id3}@test.com",
            email=f"ambassador_ag_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type = GroupType.objects.create(
            name="Marketing Team",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateAmbassadorGroup($input: CreateAmbassadorGroupInput!) {
                createAmbassadorGroup(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorGroup {
                        id
                        name
                        description
                        private
                        groupType {
                            id
                            name
                        }
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_ambassador_group_success_by_client(self):
        """Test successful ambassador group creation by client."""
        variables = {
            "input": {
                "name": "Marketing Ambassadors",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
                "description": "Marketing team ambassadors",
                "private": False,
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorGroup"]["success"] is True
        assert "created successfully" in result.data["createAmbassadorGroup"]["message"].lower(
        )
        assert result.data["createAmbassadorGroup"]["clientMutationId"] == "test-123"
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["name"] == "Marketing Ambassadors"
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["description"] == "Marketing team ambassadors"
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["private"] is False

        # Verify in DB
        @sync_to_async
        def get_group():
            return AmbassadorGroup.objects.select_related('created_by', 'group_type', 'tenant').get(
                name="Marketing Ambassadors"
            )
        group = await get_group()
        assert group.created_by == self.client_user
        assert group.group_type == self.group_type
        assert group.tenant == self.tenant

    @pytest.mark.asyncio
    async def test_create_ambassador_group_success_by_spark_admin(self):
        """Test successful ambassador group creation by spark-admin."""
        variables = {
            "input": {
                "name": "Sales Ambassadors",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
                "description": "Sales team ambassadors",
                "private": True,
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorGroup"]["success"] is True
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["name"] == "Sales Ambassadors"
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["private"] is True

    @pytest.mark.asyncio
    async def test_create_ambassador_group_minimal_fields(self):
        """Test creating ambassador group with only required fields."""
        variables = {
            "input": {
                "name": "Minimal Group",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorGroup"]["success"] is True
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["name"] == "Minimal Group"
        # Default
        assert result.data["createAmbassadorGroup"]["ambassadorGroup"]["private"] is False

    @pytest.mark.asyncio
    async def test_create_ambassador_group_unauthorized_ambassador(self):
        """Test ambassador group creation by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "name": "Unauthorized Group",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0
        assert "You do not have permission to perform this action. Client or Spark Admin access required." in str(
            result.errors[0].message)

    @pytest.mark.asyncio
    async def test_create_ambassador_group_unauthorized_anonymous(self):
        """Test ambassador group creation by unauthorized user (anonymous)."""
        variables = {
            "input": {
                "name": "Anonymous Group",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestUpdateAmbassadorGroup(AmbassadorsGraphQLTestCase):
    """Tests for update_ambassador_group mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Group Update Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_update_ag_{unique_id}@test.com",
            email=f"client_update_ag_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_update_ag_{unique_id2}@test.com",
            email=f"spark_update_ag_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_update_ag_{unique_id3}@test.com",
            email=f"ambassador_update_ag_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type = GroupType.objects.create(
            name="Original Type",
            created_by=system_user,
            updated_by=system_user,
        )
        self.group_type2 = GroupType.objects.create(
            name="New Type",
            created_by=system_user,
            updated_by=system_user,
        )

        self.ambassador_group = AmbassadorGroup.objects.create(
            name="Old Group Name",
            description="Old description",
            private=False,
            group_type=self.group_type,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation UpdateAmbassadorGroup($input: UpdateAmbassadorGroupInput!) {
                updateAmbassadorGroup(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorGroup {
                        id
                        name
                        description
                        private
                        groupType {
                            id
                            name
                        }
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_update_ambassador_group_success_by_client(self):
        """Test successful ambassador group update by client."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
                "name": "New Group Name",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type2.id),
                "description": "New description",
                "private": True,
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateAmbassadorGroup"]["success"] is True
        assert "updated successfully" in result.data["updateAmbassadorGroup"]["message"].lower(
        )
        assert result.data["updateAmbassadorGroup"]["ambassadorGroup"]["name"] == "New Group Name"
        assert result.data["updateAmbassadorGroup"]["ambassadorGroup"]["description"] == "New description"
        assert result.data["updateAmbassadorGroup"]["ambassadorGroup"]["private"] is True

        # Verify in DB
        @sync_to_async
        def get_group():
            return AmbassadorGroup.objects.select_related('updated_by', 'group_type').get(pk=self.ambassador_group.id)
        group = await get_group()
        assert group.name == "New Group Name"
        assert group.updated_by == self.client_user
        assert group.group_type == self.group_type2

    @pytest.mark.asyncio
    async def test_update_ambassador_group_success_by_spark_admin(self):
        """Test successful ambassador group update by spark-admin."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
                "name": "Updated By Spark Admin",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateAmbassadorGroup"]["success"] is True
        assert result.data["updateAmbassadorGroup"]["ambassadorGroup"]["name"] == "Updated By Spark Admin"

    @pytest.mark.asyncio
    async def test_update_ambassador_group_not_found(self):
        """Test update of non-existent ambassador group."""
        variables = {
            "input": {
                "id": "999999",
                "name": "New Name",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is not None
        assert len(result.errors) > 0
        assert "AmbassadorGroup matching query does not exist" in str(
            result.errors[0].message)

    @pytest.mark.asyncio
    async def test_update_ambassador_group_unauthorized_ambassador(self):
        """Test ambassador group update by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
                "name": "Unauthorized Update",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_update_ambassador_group_unauthorized_anonymous(self):
        """Test ambassador group update by unauthorized user (anonymous)."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
                "name": "Anonymous Update",
                "tenantId": str(self.tenant.id),
                "groupTypeId": str(self.group_type.id),
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestDeleteAmbassadorGroup(AmbassadorsGraphQLTestCase):
    """Tests for delete_ambassador_group mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Group Delete Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_delete_ag_{unique_id}@test.com",
            email=f"client_delete_ag_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_delete_ag_{unique_id2}@test.com",
            email=f"spark_delete_ag_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_delete_ag_{unique_id3}@test.com",
            email=f"ambassador_delete_ag_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type = GroupType.objects.create(
            name="Group Type for Delete",
            created_by=system_user,
            updated_by=system_user,
        )

        self.ambassador_group = AmbassadorGroup.objects.create(
            name="Group to Delete",
            description="This group will be deleted",
            private=False,
            group_type=self.group_type,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation DeleteAmbassadorGroup($input: DeleteAmbassadorGroupInput!) {
                deleteAmbassadorGroup(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_delete_ambassador_group_success_by_client(self):
        """Test successful ambassador group deletion by client."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteAmbassadorGroup"]["success"] is True
        assert "deleted successfully" in result.data["deleteAmbassadorGroup"]["message"].lower(
        )
        assert result.data["deleteAmbassadorGroup"]["clientMutationId"] == "test-123"

        # Verify in DB that it's deleted
        @sync_to_async
        def check_deleted():
            return AmbassadorGroup.objects.filter(pk=self.ambassador_group.id).exists()
        exists = await check_deleted()
        assert exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_group_success_by_spark_admin(self):
        """Test successful ambassador group deletion by spark-admin."""
        # Create another group for this test
        system_user = self.get_system_user()
        another_group = await AmbassadorGroup.objects._create(
            name="Another Group to Delete",
            group_type=self.group_type,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        variables = {
            "input": {
                "id": str(another_group.id),
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteAmbassadorGroup"]["success"] is True

    @pytest.mark.asyncio
    async def test_delete_ambassador_group_not_found(self):
        """Test deletion of non-existent ambassador group."""
        variables = {
            "input": {
                "id": "999999",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteAmbassadorGroup"]["success"] is False
        assert "AmbassadorGroup not found." in result.data["deleteAmbassadorGroup"]["message"]

    @pytest.mark.asyncio
    async def test_delete_ambassador_group_unauthorized_ambassador(self):
        """Test ambassador group deletion by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0

    @pytest.mark.asyncio
    async def test_delete_ambassador_group_unauthorized_anonymous(self):
        """Test ambassador group deletion by unauthorized user (anonymous)."""
        variables = {
            "input": {
                "id": str(self.ambassador_group.id),
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestAmbassadorGroupQueries(AmbassadorsGraphQLTestCase):
    """Tests for ambassador group queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Group Query Tenant")

        unique_id = str(uuid.uuid4())[:8]
        # Queries require IsClientOrSparkAdmin
        self.client_user = self.create_user(
            username=f"user_query_ag_{unique_id}@test.com",
            email=f"user_query_ag_{unique_id}@test.com",
            role=self.roles["client"],
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type1 = GroupType.objects.create(
            name="Type 1",
            created_by=system_user,
            updated_by=system_user,
        )
        self.group_type2 = GroupType.objects.create(
            name="Type 2",
            created_by=system_user,
            updated_by=system_user,
        )

        self.ambassador_group1 = AmbassadorGroup.objects.create(
            name="Marketing",
            description="Marketing team",
            private=False,
            group_type=self.group_type1,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )
        self.ambassador_group2 = AmbassadorGroup.objects.create(
            name="Sales",
            description="Sales team",
            private=True,
            group_type=self.group_type2,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )
        self.ambassador_group3 = AmbassadorGroup.objects.create(
            name="Support",
            description="Support team",
            private=False,
            group_type=self.group_type1,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_ambassador_groups_list_success(self):
        """Test successful ambassador groups list query."""
        query = """
            query {
                ambassadorGroups(first: 10) {
                    edges {
                        node {
                            id
                            name
                            description
                            private
                            groupType {
                                id
                                name
                            }
                        }
                    }
                    totalCount
                }
            }
        """

        result = await self._execute_query_authenticated(
            query, None, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorGroups"]["totalCount"] >= 3
        group_names = [edge["node"]["name"]
                       for edge in result.data["ambassadorGroups"]["edges"]]
        assert "Marketing" in group_names
        assert "Sales" in group_names
        assert "Support" in group_names

    @pytest.mark.asyncio
    async def test_ambassador_groups_filter_by_search(self):
        """Test ambassador groups query with search filter."""
        query = """
            query {
                ambassadorGroups(filters: { search: "Sale" }) {
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
            query, None, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorGroups"]["totalCount"] >= 1
        group_names = [edge["node"]["name"]
                       for edge in result.data["ambassadorGroups"]["edges"]]
        assert "Sales" in group_names

    @pytest.mark.asyncio
    async def test_ambassador_group_single_success(self):
        """Test successful single ambassador group query."""
        query = f"""
            query {{
                ambassadorGroup(groupId: "{self.ambassador_group1.id}") {{
                    id
                    name
                    description
                    private
                    groupType {{
                        id
                        name
                    }}
                }}
            }}
        """

        result = await self._execute_query_authenticated(
            query, None, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        decoded_id = base64.b64decode(
            result.data["ambassadorGroup"]["id"]).decode("utf-8")
        assert decoded_id == f"AmbassadorGroup:{self.ambassador_group1.id}"
        assert result.data["ambassadorGroup"]["name"] == "Marketing"
        assert result.data["ambassadorGroup"]["description"] == "Marketing team"
        assert result.data["ambassadorGroup"]["private"] is False

    @pytest.mark.asyncio
    async def test_ambassador_group_single_not_found(self):
        """Test single ambassador group query with non-existent ID."""
        query = """
            query {
                ambassadorGroup(groupId: "999999") {
                    id
                    name
                }
            }
        """

        result = await self._execute_query_authenticated(
            query, None, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorGroup"] is None

    @pytest.mark.asyncio
    async def test_ambassador_groups_unauthorized(self):
        """Test ambassador groups query by unauthorized user."""
        query = """
            query {
                ambassadorGroups(first: 10) {
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
