"""
Tests for skill and ambassador skill mutations and queries.

This module tests:
- create_skill mutation (authenticated users)
- update_skill mutation (authenticated users)
- delete_skill mutation (authenticated users)
- skills query (authenticated users)
- skill query (authenticated users)
- create_ambassador_skill mutation (client/spark-admin only)
- delete_ambassador_skill mutation (client/spark-admin only)
- ambassador_skills query (authenticated users)
- ambassador_skill query (authenticated users)
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import Skill, AmbassadorSkill, Ambassador
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from tenants.models import Tenant, TenantedUser

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestCreateSkill(AmbassadorsGraphQLTestCase):
    """Tests for create_skill mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Skill Creation Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"user_skill_{unique_id}@test.com",
            email=f"user_skill_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.user, self.tenant)

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateSkill($input: CreateSkillInput!) {
                createSkill(input: $input) {
                    success
                    message
                    clientMutationId
                    skill {
                        id
                        name
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_skill_success(self):
        """Test successful skill creation."""
        variables = {
            "input": {
                "name": "Python Programming",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createSkill"]["success"] is True
        assert "created successfully" in result.data["createSkill"]["message"].lower()
        assert result.data["createSkill"]["clientMutationId"] == "test-123"
        assert result.data["createSkill"]["skill"]["name"] == "Python Programming"

        # Verify in DB
        @sync_to_async
        def get_skill():
            return Skill.objects.select_related('created_by').get(name="Python Programming")
        skill = await get_skill()
        assert skill.created_by == self.user

    @pytest.mark.asyncio
    async def test_create_skill_with_explicit_tenant(self):
        """Test skill creation with explicit tenant_id (spark-admin)."""
        @sync_to_async
        def create_user_and_tenant():
            unique_id = str(uuid.uuid4())[:8]
            spark_admin_user = self.create_user(
                username=f"spark_admin_skill_{unique_id}@test.com",
                email=f"spark_admin_skill_{unique_id}@test.com",
                role=self.roles['spark_admin']
            )
            tenant2 = self.create_tenant(name="Skill Tenant 2")
            return spark_admin_user, tenant2
        
        spark_admin_user, tenant2 = await create_user_and_tenant()
        
        variables = {
            "input": {
                "name": "JavaScript Programming",
                "tenantId": str(tenant2.id),
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createSkill"]["success"] is True
        assert result.data["createSkill"]["skill"]["name"] == "JavaScript Programming"

    @pytest.mark.asyncio
    async def test_create_skill_unauthorized(self):
        """Test skill creation by unauthorized user."""
        variables = {
            "input": {
                "name": "Test Skill",
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestUpdateSkill(AmbassadorsGraphQLTestCase):
    """Tests for update_skill mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Skill Update Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"user_update_{unique_id}@test.com",
            email=f"user_update_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.user, self.tenant)

        system_user = self.get_system_user()
        self.skill = Skill.objects.create(
            name="Old Skill Name",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation UpdateSkill($input: UpdateSkillInput!) {
                updateSkill(input: $input) {
                    success
                    message
                    clientMutationId
                    skill {
                        id
                        name
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_update_skill_success(self):
        """Test successful skill update."""
        variables = {
            "input": {
                "id": str(self.skill.id),
                "name": "New Skill Name",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateSkill"]["success"] is True
        assert "updated successfully" in result.data["updateSkill"]["message"].lower()
        assert result.data["updateSkill"]["skill"]["name"] == "New Skill Name"

        # Verify in DB
        @sync_to_async
        def get_skill():
            return Skill.objects.select_related('updated_by').get(pk=self.skill.id)
        skill = await get_skill()
        assert skill.name == "New Skill Name"
        assert skill.updated_by == self.user

    @pytest.mark.asyncio
    async def test_update_skill_not_found(self):
        """Test update of non-existent skill."""
        variables = {
            "input": {
                "id": "999999",
                "name": "New Name",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateSkill"]["success"] is False
        assert "not found" in result.data["updateSkill"]["message"].lower()

    @pytest.mark.asyncio
    async def test_update_skill_unauthorized(self):
        """Test skill update by unauthorized user."""
        variables = {
            "input": {
                "id": str(self.skill.id),
                "name": "Unauthorized Update",
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestDeleteSkill(AmbassadorsGraphQLTestCase):
    """Tests for delete_skill mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Skill Delete Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"user_delete_{unique_id}@test.com",
            email=f"user_delete_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.user, self.tenant)

        system_user = self.get_system_user()
        self.skill = Skill.objects.create(
            name="Skill To Delete",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation DeleteSkill($input: DeleteSkillInput!) {
                deleteSkill(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_delete_skill_success(self):
        """Test successful skill deletion."""
        variables = {
            "input": {
                "id": str(self.skill.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteSkill"]["success"] is True
        assert "deleted successfully" in result.data["deleteSkill"]["message"].lower()

        # Verify deleted in DB
        skill_exists = await sync_to_async(Skill.objects.filter(pk=self.skill.id).exists)()
        assert skill_exists is False

    @pytest.mark.asyncio
    async def test_delete_skill_not_found(self):
        """Test deletion of non-existent skill."""
        variables = {
            "input": {
                "id": "999999",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteSkill"]["success"] is False
        assert "not found" in result.data["deleteSkill"]["message"].lower()

    @pytest.mark.asyncio
    async def test_delete_skill_unauthorized(self):
        """Test skill deletion by unauthorized user."""
        variables = {
            "input": {
                "id": str(self.skill.id),
            }
        }

        from django.contrib.auth.models import AnonymousUser
        result = await self._execute_mutation_authenticated(
            self.mutation, variables, AnonymousUser(), self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None


@pytest.mark.django_db(transaction=True)
class TestSkillQueries(AmbassadorsGraphQLTestCase):
    """Tests for skill queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Skill Query Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"user_query_{unique_id}@test.com",
            email=f"user_query_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.user, self.tenant)

        system_user = self.get_system_user()
        self.skill1 = Skill.objects.create(
            name="Python",
            created_by=system_user,
            updated_by=system_user,
        )
        self.skill2 = Skill.objects.create(
            name="JavaScript",
            created_by=system_user,
            updated_by=system_user,
        )
        self.skill3 = Skill.objects.create(
            name="React",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_skills_list_success(self):
        """Test successful skills list query."""
        query = """
            query {
                skills(first: 10) {
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
        assert result.data["skills"]["totalCount"] >= 3
        skill_names = [edge["node"]["name"] for edge in result.data["skills"]["edges"]]
        assert "Python" in skill_names
        assert "JavaScript" in skill_names
        assert "React" in skill_names

    @pytest.mark.asyncio
    async def test_skills_filter_by_search(self):
        """Test skills query with search filter."""
        query = """
            query {
                skills(filters: { search: "Java" }) {
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
        assert result.data["skills"]["totalCount"] >= 1
        skill_names = [edge["node"]["name"] for edge in result.data["skills"]["edges"]]
        assert "JavaScript" in skill_names

    @pytest.mark.asyncio
    async def test_skill_single_success(self):
        """Test successful single skill query."""
        query = f"""
            query {{
                skill(skillId: "{self.skill1.id}") {{
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
        assert result.data["skill"]["id"] == str(self.skill1.id)
        assert result.data["skill"]["name"] == "Python"

    @pytest.mark.asyncio
    async def test_skill_single_not_found(self):
        """Test single skill query with non-existent ID."""
        query = """
            query {
                skill(skillId: "999999") {
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
        assert result.data["skill"] is None

    @pytest.mark.asyncio
    async def test_skills_unauthorized(self):
        """Test skills query by unauthorized user."""
        query = """
            query {
                skills(first: 10) {
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


@pytest.mark.django_db(transaction=True)
class TestCreateAmbassadorSkill(AmbassadorsGraphQLTestCase):
    """Tests for create_ambassador_skill mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Skill Creation Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_skill_{unique_id}@test.com",
            email=f"client_skill_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_skill_{unique_id2}@test.com",
            email=f"spark_skill_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        # Spark admin needs tenant for tenant resolution, but can work across tenants
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_skill_{unique_id3}@test.com",
            email=f"ambassador_skill_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        system_user = self.get_system_user()
        self.skill = Skill.objects.create(
            name="Python Programming",
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateAmbassadorSkill($input: CreateAmbassadorSkillInput!) {
                createAmbassadorSkill(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorSkill {
                        id
                        ambassadorId
                        skillId
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_success_by_client(self):
        """Test successful ambassador skill creation by client."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "skillId": str(self.skill.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorSkill"]["success"] is True
        assert "created successfully" in result.data["createAmbassadorSkill"]["message"].lower()
        assert result.data["createAmbassadorSkill"]["ambassadorSkill"]["ambassadorId"] == str(self.ambassador.id)
        assert result.data["createAmbassadorSkill"]["ambassadorSkill"]["skillId"] == str(self.skill.id)

        # Verify in DB
        ambassador_skill = await sync_to_async(
            AmbassadorSkill.objects.select_related('created_by').get
        )(
            ambassador=self.ambassador,
            skill=self.skill
        )
        assert ambassador_skill.created_by == self.client_user

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_success_by_spark_admin(self):
        """Test successful ambassador skill creation by spark-admin."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "skillId": str(self.skill.id),
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorSkill"]["success"] is True

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_duplicate(self):
        """Test creating duplicate ambassador skill."""
        # Create first assignment
        @sync_to_async
        def create_first_assignment():
            system_user = self.get_system_user()
            return AmbassadorSkill.objects.create(
                ambassador=self.ambassador,
                skill=self.skill,
                created_by=system_user,
                updated_by=system_user,
            )
        await create_first_assignment()

        # Try to create duplicate
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "skillId": str(self.skill.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorSkill"]["success"] is False
        assert "already has this skill" in result.data["createAmbassadorSkill"]["message"].lower()

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_ambassador_not_found(self):
        """Test creating ambassador skill with non-existent ambassador."""
        variables = {
            "input": {
                "ambassadorId": "999999",
                "skillId": str(self.skill.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorSkill"]["success"] is False
        assert "ambassador not found" in result.data["createAmbassadorSkill"]["message"].lower()

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_skill_not_found(self):
        """Test creating ambassador skill with non-existent skill."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "skillId": "999999",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorSkill"]["success"] is False
        assert "skill not found" in result.data["createAmbassadorSkill"]["message"].lower()

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_without_skill_tenant(self):
        """Skill no longer belongs to tenant, so assignment is validated by ambassador tenant only."""
        @sync_to_async
        def create_skill():
            system_user = self.get_system_user()
            skill2 = Skill.objects.create(
                name="Different Skill",
                created_by=system_user,
                updated_by=system_user,
            )
            return skill2
        skill2 = await create_skill()

        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "skillId": str(skill2.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorSkill"]["success"] is True

    @pytest.mark.asyncio
    async def test_create_ambassador_skill_unauthorized(self):
        """Test ambassador skill creation by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "skillId": str(self.skill.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestDeleteAmbassadorSkill(AmbassadorsGraphQLTestCase):
    """Tests for delete_ambassador_skill mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Skill Delete Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_delete_{unique_id}@test.com",
            email=f"client_delete_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_delete_{unique_id2}@test.com",
            email=f"ambassador_delete_{unique_id2}@test.com",
            role=self.roles['ambassador'],
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        system_user = self.get_system_user()
        self.skill = Skill.objects.create(
            name="Python Programming",
            created_by=system_user,
            updated_by=system_user,
        )

        self.ambassador_skill = AmbassadorSkill.objects.create(
            ambassador=self.ambassador,
            skill=self.skill,
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation DeleteAmbassadorSkill($input: DeleteAmbassadorSkillInput!) {
                deleteAmbassadorSkill(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_delete_ambassador_skill_success(self):
        """Test successful ambassador skill deletion."""
        variables = {
            "input": {
                "ambassadorSkillId": str(self.ambassador_skill.id),
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteAmbassadorSkill"]["success"] is True
        assert "deleted successfully" in result.data["deleteAmbassadorSkill"]["message"].lower()

        # Verify deleted in DB
        exists = await sync_to_async(
            AmbassadorSkill.objects.filter(pk=self.ambassador_skill.id).exists
        )()
        assert exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_skill_not_found(self):
        """Test deletion of non-existent ambassador skill."""
        variables = {
            "input": {
                "ambassadorSkillId": "999999",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteAmbassadorSkill"]["success"] is False
        assert "not found" in result.data["deleteAmbassadorSkill"]["message"].lower()

    @pytest.mark.asyncio
    async def test_delete_ambassador_skill_unauthorized(self):
        """Test ambassador skill deletion by unauthorized user (ambassador)."""
        variables = {
            "input": {
                "ambassadorSkillId": str(self.ambassador_skill.id),
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestAmbassadorSkillQueries(AmbassadorsGraphQLTestCase):
    """Tests for ambassador skill queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Ambassador Skill Query Tenant")
        
        unique_id = str(uuid.uuid4())[:8]
        self.user = self.create_user(
            username=f"user_query_skill_{unique_id}@test.com",
            email=f"user_query_skill_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.ambassador_user1 = self.create_user(
            username=f"ambassador1_skill_{unique_id2}@test.com",
            email=f"ambassador1_skill_{unique_id2}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user1, self.tenant)
        self.ambassador1 = self.create_ambassador(
            self.ambassador_user1,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user2 = self.create_user(
            username=f"ambassador2_skill_{unique_id3}@test.com",
            email=f"ambassador2_skill_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user2, self.tenant)
        self.ambassador2 = self.create_ambassador(
            self.ambassador_user2,
            address="456 Test St",
            coordinates=[40.7580, -73.9855],
            is_active=True,
        )

        system_user = self.get_system_user()
        self.skill1 = Skill.objects.create(
            name="Python",
            created_by=system_user,
            updated_by=system_user,
        )
        self.skill2 = Skill.objects.create(
            name="JavaScript",
            created_by=system_user,
            updated_by=system_user,
        )

        self.ambassador_skill1 = AmbassadorSkill.objects.create(
            ambassador=self.ambassador1,
            skill=self.skill1,
            created_by=system_user,
            updated_by=system_user,
        )
        self.ambassador_skill2 = AmbassadorSkill.objects.create(
            ambassador=self.ambassador1,
            skill=self.skill2,
            created_by=system_user,
            updated_by=system_user,
        )
        self.ambassador_skill3 = AmbassadorSkill.objects.create(
            ambassador=self.ambassador2,
            skill=self.skill1,
            created_by=system_user,
            updated_by=system_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

    @pytest.mark.asyncio
    async def test_ambassador_skills_list_success(self):
        """Test successful ambassador skills list query."""
        query = """
            query {
                ambassadorSkills(first: 10) {
                    edges {
                        node {
                            id
                            ambassadorId
                            skillId
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
        assert result.data["ambassadorSkills"]["totalCount"] >= 3

    @pytest.mark.asyncio
    async def test_ambassador_skills_filter_by_ambassador(self):
        """Test ambassador skills query with ambassador filter."""
        query = f"""
            query {{
                ambassadorSkills(filters: {{ ambassadorId: "{self.ambassador1.id}" }}) {{
                    edges {{
                        node {{
                            ambassadorId
                            skillId
                        }}
                    }}
                    totalCount
                }}
            }}
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorSkills"]["totalCount"] >= 2
        for edge in result.data["ambassadorSkills"]["edges"]:
            assert edge["node"]["ambassadorId"] == str(self.ambassador1.id)

    @pytest.mark.asyncio
    async def test_ambassador_skills_filter_by_skill(self):
        """Test ambassador skills query with skill filter."""
        query = f"""
            query {{
                ambassadorSkills(filters: {{ skillId: "{self.skill1.id}" }}) {{
                    edges {{
                        node {{
                            ambassadorId
                            skillId
                        }}
                    }}
                    totalCount
                }}
            }}
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorSkills"]["totalCount"] >= 2
        for edge in result.data["ambassadorSkills"]["edges"]:
            assert edge["node"]["skillId"] == str(self.skill1.id)

    @pytest.mark.asyncio
    async def test_ambassador_skill_single_success(self):
        """Test successful single ambassador skill query."""
        query = f"""
            query {{
                ambassadorSkill(ambassadorSkillId: "{self.ambassador_skill1.id}") {{
                    id
                    ambassadorId
                    skillId
                }}
            }}
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorSkill"]["id"] == str(self.ambassador_skill1.id)
        assert result.data["ambassadorSkill"]["ambassadorId"] == str(self.ambassador1.id)
        assert result.data["ambassadorSkill"]["skillId"] == str(self.skill1.id)

    @pytest.mark.asyncio
    async def test_ambassador_skill_single_not_found(self):
        """Test single ambassador skill query with non-existent ID."""
        query = """
            query {
                ambassadorSkill(ambassadorSkillId: "999999") {
                    id
                }
            }
        """

        result = await self._execute_query_authenticated(
            query, None, self.user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorSkill"] is None

    @pytest.mark.asyncio
    async def test_ambassador_skills_tenant_scoping(self):
        """Test ambassador skills query respects tenant scoping."""
        @sync_to_async
        def create_other_tenant_data():
            tenant2 = self.create_tenant(name="Tenant 2")
            system_user = self.get_system_user()
            ambassador3 = self.create_ambassador(
                self.user,
                address="789 Test St",
                coordinates=[40.7505, -73.9934],
                is_active=True,
            )
            skill3 = Skill.objects.create(
                name="React",
                created_by=system_user,
                updated_by=system_user,
            )
            AmbassadorSkill.objects.create(
                ambassador=ambassador3,
                skill=skill3,
                created_by=system_user,
                updated_by=system_user,
            )
            return tenant2
        await create_other_tenant_data()

        query = """
            query {
                ambassadorSkills(first: 10) {
                    edges {
                        node {
                            id
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
        # Should only see ambassador skills from user's tenant.
        assert result.data["ambassadorSkills"]["totalCount"] == 3

    @pytest.mark.asyncio
    async def test_ambassador_skills_unauthorized(self):
        """Test ambassador skills query by unauthorized user."""
        query = """
            query {
                ambassadorSkills(first: 10) {
                    edges {
                        node {
                            id
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
