"""
Tests for add_ambassadors_to_group and remove_ambassadors_from_group mutations.

This module tests:
- add_ambassadors_to_group mutation (client/spark-admin only)
  - Adding ambassadors to existing group
  - Adding ambassadors with job (creates AmbassadorJob records)
  - Adding ambassadors without job (only creates UserGroup records)
  - Group validation
  - Ambassador validation
  - Job validation (if provided)
- remove_ambassadors_from_group mutation (client/spark-admin only)
  - Removing ambassadors from group
  - UserGroup validation
  - Group validation
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from ambassadors.models import AmbassadorGroup, GroupType, UserGroup
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from jobs import models as job_models

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestAddAmbassadorsToGroup(AmbassadorsGraphQLTestCase):
    """Tests for add_ambassadors_to_group mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(
            name="Add Ambassadors To Group Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_add_{unique_id}@test.com",
            email=f"client_add_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_add_{unique_id2}@test.com",
            email=f"spark_add_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_add_{unique_id3}@test.com",
            email=f"ambassador_add_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type = GroupType.objects.create(
            name="Marketing Team",
            created_by=system_user,
            updated_by=system_user,
        )

        # Create an existing group
        self.existing_group = AmbassadorGroup.objects.create(
            name="Existing Group",
            description="Test group",
            private=False,
            group_type=self.group_type,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        # Create ambassadors
        self.ambassador_user1 = self.create_user(
            username=f"amb1_add_{unique_id}@test.com",
            email=f"amb1_add_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user1, self.tenant)
        self.ambassador1 = self.create_ambassador(user=self.ambassador_user1)

        self.ambassador_user2 = self.create_user(
            username=f"amb2_add_{unique_id}@test.com",
            email=f"amb2_add_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user2, self.tenant)
        self.ambassador2 = self.create_ambassador(user=self.ambassador_user2)

        # Create job-related test data
        self.company = self.create_company(
            name="Test Company",
            email="company@test.com",
            phone="1234567890",
            tenant=self.tenant
        )
        self.event = self.create_event(
            name="Test Event",
            tenant=self.tenant,
            address="123 Test St"
        )
        self.job_title = job_models.JobTitle.objects.create(
            name="Promoter",
            tenant=self.tenant,
            created_by=system_user
        )
        self.rate_type = job_models.RateType.objects.create(
            name="Hourly",
            tenant=self.tenant,
            created_by=system_user
        )
        self.rate = job_models.Rate.objects.create(
            amount=75.0,
            rate_type=self.rate_type,
            tenant=self.tenant,
            created_by=system_user
        )
        self.job = job_models.Job.objects.create(
            name="Test Job",
            code="JOB-ADD-001",
            address="123 Test St",
            company=self.company,
            event=self.event,
            job_title=self.job_title,
            tenant=self.tenant,
            rate=self.rate,
            created_by=system_user
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation AddAmbassadorsToGroup($input: AddAmbassadorsToGroupInput!) {
                addAmbassadorsToGroup(input: $input) {
                    success
                    message
                    clientMutationId
                    members {
                        id
                        uuid
                        user {
                            id
                            email
                        }
                        ambassador {
                            id
                            uuid
                        }
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_success_by_client(self):
        """Test successful addition of ambassadors to group by client."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "ambassadorIds": [str(self.ambassador1.id), str(self.ambassador2.id)],
                "clientMutationId": "add-1"
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is True
        assert "added" in result.data["addAmbassadorsToGroup"]["message"].lower()
        assert result.data["addAmbassadorsToGroup"]["clientMutationId"] == "add-1"

        members = result.data["addAmbassadorsToGroup"]["members"]
        assert len(members) == 2

        # Verify in database
        @sync_to_async
        def check_user_groups():
            return list(UserGroup.objects.filter(
                group=self.existing_group,
                ambassador__in=[self.ambassador1, self.ambassador2]
            ).select_related('user', 'ambassador'))

        db_user_groups = await check_user_groups()
        assert len(db_user_groups) == 2
        ambassador_ids = {ug.ambassador.id for ug in db_user_groups}
        assert self.ambassador1.id in ambassador_ids
        assert self.ambassador2.id in ambassador_ids

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_with_job(self):
        """Test adding ambassadors to group with job creates AmbassadorJob records."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "jobId": str(self.job.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is True

        # Verify UserGroup was created
        @sync_to_async
        def check_user_group():
            return UserGroup.objects.filter(
                group=self.existing_group,
                ambassador=self.ambassador1
            ).exists()

        user_group_exists = await check_user_group()
        assert user_group_exists is True

        # Verify AmbassadorJob was created
        @sync_to_async
        def check_ambassador_job():
            aj = job_models.AmbassadorJob.objects.filter(
                ambassador=self.ambassador1,
                job=self.job
            ).select_related('status', 'rate').first()
            if aj:
                return {
                    'status_slug': aj.status.slug,
                    'rate': aj.rate,
                    'appear_as_rfp': aj.appear_as_rfp,
                }
            return None

        ambassador_job_data = await check_ambassador_job()
        assert ambassador_job_data is not None
        assert ambassador_job_data['status_slug'] == "invited"
        assert ambassador_job_data['rate'] == self.rate
        assert ambassador_job_data['appear_as_rfp'] is True

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_success_by_spark_admin(self):
        """Test successful addition by spark admin."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is True

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_group_not_found(self):
        """Test error when group doesn't exist."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": "999999",
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is False
        message_lower = result.data["addAmbassadorsToGroup"]["message"].lower()
        assert "not found" in message_lower or "does not exist" in message_lower

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_ambassadors_not_found(self):
        """Test error when ambassadors don't exist."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "ambassadorIds": ["999999", "999998"],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is False
        message_lower = result.data["addAmbassadorsToGroup"]["message"].lower()
        assert "not found" in message_lower or "does not exist" in message_lower

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_job_not_found(self):
        """Test error when job doesn't exist."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "jobId": "999999",
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is False
        message_lower = result.data["addAmbassadorsToGroup"]["message"].lower()
        assert "not found" in message_lower or "does not exist" in message_lower

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_empty_list(self):
        """Test adding empty list of ambassadors returns empty result."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "ambassadorIds": [],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is True
        assert result.data["addAmbassadorsToGroup"]["members"] == []

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_unauthorized_ambassador(self):
        """Test ambassador users cannot add ambassadors to group."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path)

        assert result.data is None
        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_add_ambassadors_to_group_single_ambassador(self):
        """Test adding a single ambassador."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "ambassadorIds": [str(self.ambassador1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["addAmbassadorsToGroup"]["success"] is True
        assert len(result.data["addAmbassadorsToGroup"]["members"]) == 1


@pytest.mark.django_db(transaction=True)
class TestRemoveAmbassadorsFromGroup(AmbassadorsGraphQLTestCase):
    """Tests for remove_ambassadors_from_group mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(
            name="Remove Ambassadors From Group Tenant")

        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_remove_{unique_id}@test.com",
            email=f"client_remove_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_remove_{unique_id2}@test.com",
            email=f"spark_remove_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )
        self.create_tenanted_user(self.spark_admin_user, self.tenant)

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_remove_{unique_id3}@test.com",
            email=f"ambassador_remove_{unique_id3}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)

        system_user = self.get_system_user()
        self.group_type = GroupType.objects.create(
            name="Marketing Team",
            created_by=system_user,
            updated_by=system_user,
        )

        # Create an existing group
        self.existing_group = AmbassadorGroup.objects.create(
            name="Existing Group",
            description="Test group",
            private=False,
            group_type=self.group_type,
            tenant=self.tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        # Create ambassadors
        self.ambassador_user1 = self.create_user(
            username=f"amb1_remove_{unique_id}@test.com",
            email=f"amb1_remove_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user1, self.tenant)
        self.ambassador1 = self.create_ambassador(user=self.ambassador_user1)

        self.ambassador_user2 = self.create_user(
            username=f"amb2_remove_{unique_id}@test.com",
            email=f"amb2_remove_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        self.create_tenanted_user(self.ambassador_user2, self.tenant)
        self.ambassador2 = self.create_ambassador(user=self.ambassador_user2)

        # Create UserGroup records
        self.user_group1 = UserGroup.objects.create(
            group=self.existing_group,
            user=self.ambassador_user1,
            ambassador=self.ambassador1,
        )
        self.user_group2 = UserGroup.objects.create(
            group=self.existing_group,
            user=self.ambassador_user2,
            ambassador=self.ambassador2,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation RemoveAmbassadorsFromGroup($input: RemoveAmbassadorsFromGroupInput!) {
                removeAmbassadorsFromGroup(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorGroup {
                        id
                        name
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_success_by_client(self):
        """Test successful removal of ambassadors from group by client."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": [str(self.user_group1.id)],
                "clientMutationId": "remove-1"
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is True
        assert "removed" in result.data["removeAmbassadorsFromGroup"]["message"].lower()
        assert result.data["removeAmbassadorsFromGroup"]["clientMutationId"] == "remove-1"

        # Verify UserGroup was deleted
        @sync_to_async
        def check_user_group():
            return UserGroup.objects.filter(id=self.user_group1.id).exists()

        user_group_exists = await check_user_group()
        assert user_group_exists is False

        # Verify other UserGroup still exists
        @sync_to_async
        def check_other_user_group():
            return UserGroup.objects.filter(id=self.user_group2.id).exists()

        other_user_group_exists = await check_other_user_group()
        assert other_user_group_exists is True

    @pytest.mark.asyncio
    async def test_remove_multiple_ambassadors_from_group(self):
        """Test removing multiple ambassadors from group."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": [str(self.user_group1.id), str(self.user_group2.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is True

        # Verify both UserGroups were deleted
        @sync_to_async
        def check_user_groups():
            return UserGroup.objects.filter(
                id__in=[self.user_group1.id, self.user_group2.id]
            ).count()

        count = await check_user_groups()
        assert count == 0

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_success_by_spark_admin(self):
        """Test successful removal by spark admin."""
        # Recreate user_group1 since it was deleted in previous test
        @sync_to_async
        def recreate_user_group():
            return UserGroup.objects.create(
                group=self.existing_group,
                user=self.ambassador_user1,
                ambassador=self.ambassador1,
            )

        user_group = await recreate_user_group()

        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": [str(user_group.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is True

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_group_not_found(self):
        """Test error when group doesn't exist."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": "999999",
                "userGroupIds": [str(self.user_group1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is False
        assert "not found" in result.data["removeAmbassadorsFromGroup"]["message"].lower()

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_user_group_not_found(self):
        """Test error when UserGroup doesn't exist."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": ["999999"],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is False
        assert "not found" in result.data["removeAmbassadorsFromGroup"]["message"].lower()

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_missing_user_group_ids(self):
        """Test error when user_group_ids is empty."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": [],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is False
        assert "required" in result.data["removeAmbassadorsFromGroup"]["message"].lower()

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_unauthorized_ambassador(self):
        """Test ambassador users cannot remove ambassadors from group."""
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": [str(self.user_group1.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path)

        assert result.data is None
        assert result.errors is not None

    @pytest.mark.asyncio
    async def test_remove_ambassadors_from_group_wrong_group(self):
        """Test error when UserGroup belongs to different group."""
        # Create another group and user_group
        @sync_to_async
        def create_other_group_and_user_group():
            system_user = self.get_system_user()
            other_group = AmbassadorGroup.objects.create(
                name="Other Group",
                description="Other test group",
                private=False,
                group_type=self.group_type,
                tenant=self.tenant,
                created_by=system_user,
                updated_by=system_user,
            )
            other_user_group = UserGroup.objects.create(
                group=other_group,
                user=self.ambassador_user1,
                ambassador=self.ambassador1,
            )
            return other_group, other_user_group

        other_group, other_user_group = await create_other_group_and_user_group()

        # Try to remove user_group from other_group using existing_group id
        variables = {
            "input": {
                "tenantId": str(self.tenant.id),
                "groupId": str(self.existing_group.id),
                "userGroupIds": [str(other_user_group.id)],
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path)

        assert result.errors is None
        assert result.data is not None
        assert result.data["removeAmbassadorsFromGroup"]["success"] is False
        assert "not found" in result.data["removeAmbassadorsFromGroup"]["message"].lower()

