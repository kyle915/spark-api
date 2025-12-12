"""
Tests for ambassador review mutations and queries.

This module tests:
- create_ambassador_review mutation (client/spark-admin only)
- update_ambassador_review mutation (client/spark-admin only)
- delete_ambassador_review mutation (client/spark-admin only)
- ambassador_reviews query (authenticated users)
- ambassador_review query (authenticated users)
"""
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta, datetime

from ambassadors.models import Ambassador, AmbassadorReview
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from events.models import Client
from tenants.models import Tenant, TenantedUser
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestCreateAmbassadorReview(AmbassadorsGraphQLTestCase):
    """Tests for create_ambassador_review mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Review Creation Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_review_{unique_id}@test.com",
            email=f"client_review_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_review_{unique_id2}@test.com",
            email=f"spark_review_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_review_{unique_id3}@test.com",
            email=f"ambassador_review_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        # Create client
        self.client = self.create_client(
            name="Test Client",
            email=f"client_test_{unique_id}@test.com",
            tenant=self.tenant,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateAmbassadorReview($input: CreateAmbassadorReviewInput!) {
                createAmbassadorReview(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorReview {
                        id
                        review
                        score
                        ambassadorId
                        clientId
                        tenantId
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_ambassador_review_success_by_client(self):
        """Test successful review creation by client."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "review": "Great ambassador, very professional!",
                "score": 5,
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is True
        assert "created successfully" in result.data["createAmbassadorReview"]["message"].lower(
        )
        assert result.data["createAmbassadorReview"]["clientMutationId"] == "test-123"
        assert result.data["createAmbassadorReview"]["ambassadorReview"]["score"] == 5
        assert result.data["createAmbassadorReview"]["ambassadorReview"]["review"] == "Great ambassador, very professional!"

        # Verify in DB
        @sync_to_async
        def get_review():
            return AmbassadorReview.objects.select_related('tenant', 'created_by').get(
                ambassador=self.ambassador,
                client=self.client
            )
        review = await get_review()
        assert review.score == 5
        assert review.review == "Great ambassador, very professional!"
        assert review.tenant == self.tenant
        assert review.created_by == self.client_user

    @pytest.mark.asyncio
    async def test_create_ambassador_review_success_by_spark_admin(self):
        """Test successful review creation by spark admin."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "review": "Excellent work!",
                "score": 4,
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is True

    @pytest.mark.asyncio
    async def test_create_ambassador_review_without_score(self):
        """Test creating review without score (should be allowed)."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "review": "Good ambassador",
                "clientMutationId": "test-789",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is True
        assert result.data["createAmbassadorReview"]["ambassadorReview"]["score"] is None

    @pytest.mark.asyncio
    async def test_create_ambassador_review_without_review_text(self):
        """Test creating review without review text (should be allowed)."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "score": 4,
                "clientMutationId": "test-101",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is True
        assert result.data["createAmbassadorReview"]["ambassadorReview"]["review"] is None

    @pytest.mark.asyncio
    async def test_create_ambassador_review_invalid_score_too_high(self):
        """Test creating review with score > 5 (should fail)."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "score": 6,
                "clientMutationId": "test-invalid-high",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is False
        assert "between 1 and 5" in result.data["createAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_ambassador_review_invalid_score_too_low(self):
        """Test creating review with score < 1 (should fail)."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "score": 0,
                "clientMutationId": "test-invalid-low",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is False
        assert "between 1 and 5" in result.data["createAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_ambassador_review_duplicate(self):
        """Test creating duplicate review (same client + ambassador)."""
        # Create first review
        await sync_to_async(AmbassadorReview.objects.create)(
            ambassador=self.ambassador,
            client=self.client,
            tenant=self.tenant,
            review="First review",
            score=5,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        # Try to create duplicate
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "review": "Second review",
                "score": 4,
                "clientMutationId": "test-duplicate",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is False
        assert "already exists" in result.data["createAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_ambassador_review_ambassador_not_found(self):
        """Test creating review for non-existent ambassador."""
        variables = {
            "input": {
                "ambassadorId": "99999",
                "clientId": str(self.client.id),
                "review": "Test review",
                "score": 5,
                "clientMutationId": "test-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is False
        assert "not found" in result.data["createAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_ambassador_review_client_not_found(self):
        """Test creating review with non-existent client."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": "99999",
                "review": "Test review",
                "score": 5,
                "clientMutationId": "test-client-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorReview"]["success"] is False
        assert "not found" in result.data["createAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_ambassador_review_unauthorized(self):
        """Test creating review by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user2 = await sync_to_async(self.create_user)(
            username=f"ambassador2_review_{unique_id}@test.com",
            email=f"ambassador2_review_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "clientId": str(self.client.id),
                "review": "Unauthorized review",
                "score": 5,
                "clientMutationId": "test-unauthorized",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user2, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestUpdateAmbassadorReview(AmbassadorsGraphQLTestCase):
    """Tests for update_ambassador_review mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Review Update Tenant")
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

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_update_{unique_id3}@test.com",
            email=f"ambassador_update_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        self.client = self.create_client(
            name="Test Client",
            email=f"client_update_{unique_id}@test.com",
            tenant=self.tenant,
        )

        # Create existing review
        self.review = AmbassadorReview.objects.create(
            ambassador=self.ambassador,
            client=self.client,
            tenant=self.tenant,
            review="Original review",
            score=3,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation UpdateAmbassadorReview($input: UpdateAmbassadorReviewInput!) {
                updateAmbassadorReview(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorReview {
                        id
                        review
                        score
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_update_ambassador_review_success_by_client(self):
        """Test successful review update by client."""
        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "review": "Updated review text",
                "score": 5,
                "clientMutationId": "test-update-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateAmbassadorReview"]["success"] is True
        assert "updated successfully" in result.data["updateAmbassadorReview"]["message"].lower(
        )
        assert result.data["updateAmbassadorReview"]["ambassadorReview"]["review"] == "Updated review text"
        assert result.data["updateAmbassadorReview"]["ambassadorReview"]["score"] == 5

        # Verify in DB
        @sync_to_async
        def get_review():
            return AmbassadorReview.objects.select_related('updated_by').get(pk=self.review.id)
        review = await get_review()
        assert review.review == "Updated review text"
        assert review.score == 5
        assert review.updated_by == self.client_user

    @pytest.mark.asyncio
    async def test_update_ambassador_review_success_by_spark_admin(self):
        """Test successful review update by spark admin."""
        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "review": "Spark admin updated review",
                "clientMutationId": "test-update-spark",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorReview"]["success"] is True

        # Verify in DB
        @sync_to_async
        def get_review():
            return AmbassadorReview.objects.select_related('updated_by').get(pk=self.review.id)
        review = await get_review()
        assert review.review == "Spark admin updated review"
        assert review.updated_by == self.spark_admin_user

    @pytest.mark.asyncio
    async def test_update_ambassador_review_partial_update(self):
        """Test updating only review text."""
        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "review": "Only text updated",
                "clientMutationId": "test-partial",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorReview"]["success"] is True

        # Verify only review text changed, score unchanged
        @sync_to_async
        def get_review():
            return AmbassadorReview.objects.get(pk=self.review.id)
        review = await get_review()
        assert review.review == "Only text updated"
        assert review.score == 3  # Original value

    @pytest.mark.asyncio
    async def test_update_ambassador_review_score_only(self):
        """Test updating only score."""
        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "score": 5,
                "clientMutationId": "test-score-only",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorReview"]["success"] is True

        # Verify only score changed
        @sync_to_async
        def get_review():
            return AmbassadorReview.objects.get(pk=self.review.id)
        review = await get_review()
        assert review.score == 5
        assert review.review == "Original review"  # Original value

    @pytest.mark.asyncio
    async def test_update_ambassador_review_invalid_score(self):
        """Test updating review with invalid score."""
        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "score": 6,
                "clientMutationId": "test-invalid-score",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorReview"]["success"] is False
        assert "between 1 and 5" in result.data["updateAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_update_ambassador_review_not_found(self):
        """Test updating non-existent review."""
        variables = {
            "input": {
                "reviewId": "99999",
                "review": "Updated review",
                "clientMutationId": "test-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorReview"]["success"] is False
        assert "not found" in result.data["updateAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_update_ambassador_review_unauthorized(self):
        """Test updating review by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user2 = await sync_to_async(self.create_user)(
            username=f"ambassador2_update_{unique_id}@test.com",
            email=f"ambassador2_update_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "review": "Unauthorized update",
                "clientMutationId": "test-unauthorized",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user2, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestDeleteAmbassadorReview(AmbassadorsGraphQLTestCase):
    """Tests for delete_ambassador_review mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Review Delete Tenant")
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_delete_{unique_id}@test.com",
            email=f"client_delete_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_delete_{unique_id2}@test.com",
            email=f"spark_delete_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_delete_{unique_id3}@test.com",
            email=f"ambassador_delete_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        self.client = self.create_client(
            name="Test Client",
            email=f"client_delete_{unique_id}@test.com",
            tenant=self.tenant,
        )

        # Create review to delete
        self.review = AmbassadorReview.objects.create(
            ambassador=self.ambassador,
            client=self.client,
            tenant=self.tenant,
            review="Review to delete",
            score=4,
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation DeleteAmbassadorReview($input: DeleteAmbassadorReviewInput!) {
                deleteAmbassadorReview(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_delete_ambassador_review_success_by_client(self):
        """Test successful review deletion by client."""
        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "clientMutationId": "test-delete-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteAmbassadorReview"]["success"] is True
        assert "deleted successfully" in result.data["deleteAmbassadorReview"]["message"].lower(
        )

        # Verify review was deleted
        review_exists = await sync_to_async(
            AmbassadorReview.objects.filter(pk=self.review.id).exists
        )()
        assert review_exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_review_success_by_spark_admin(self):
        """Test successful review deletion by spark admin."""
        # Create another review for deletion
        review2 = await sync_to_async(AmbassadorReview.objects.create)(
            ambassador=self.ambassador,
            client=self.client,
            tenant=self.tenant,
            review="Another review",
            score=5,
            created_by=self.spark_admin_user,
            updated_by=self.spark_admin_user,
        )

        variables = {
            "input": {
                "reviewId": str(review2.id),
                "clientMutationId": "test-delete-spark",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteAmbassadorReview"]["success"] is True

        # Verify review was deleted
        review_exists = await sync_to_async(
            AmbassadorReview.objects.filter(pk=review2.id).exists
        )()
        assert review_exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_review_not_found(self):
        """Test deletion of non-existent review."""
        variables = {
            "input": {
                "reviewId": "99999",
                "clientMutationId": "test-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteAmbassadorReview"]["success"] is False
        assert "not found" in result.data["deleteAmbassadorReview"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_delete_ambassador_review_unauthorized(self):
        """Test review deletion by unauthorized user (ambassador)."""
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user2 = await sync_to_async(self.create_user)(
            username=f"ambassador2_delete_{unique_id}@test.com",
            email=f"ambassador2_delete_{unique_id}@test.com",
            role=self.roles['ambassador']
        )

        variables = {
            "input": {
                "reviewId": str(self.review.id),
                "clientMutationId": "test-unauthorized",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, ambassador_user2, self.endpoint_path
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestAmbassadorReviewQueries(AmbassadorsGraphQLTestCase):
    """Tests for ambassador review queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        from config.schema_client import schema_clients
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Review Query Tenant")
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_query_{unique_id}@test.com",
            email=f"client_query_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_query_{unique_id2}@test.com",
            email=f"spark_query_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_query_{unique_id3}@test.com",
            email=f"ambassador_query_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        # Create another ambassador
        unique_id4 = str(uuid.uuid4())[:8]
        self.ambassador_user2 = self.create_user(
            username=f"ambassador2_query_{unique_id4}@test.com",
            email=f"ambassador2_query_{unique_id4}@test.com",
            role=self.roles['ambassador'],
        )
        self.ambassador2 = self.create_ambassador(
            self.ambassador_user2,
            address="456 Test Ave",
            is_active=True,
        )

        # Create clients
        self.client = self.create_client(
            name="Test Client",
            email=f"client_query_{unique_id}@test.com",
            tenant=self.tenant,
        )

        unique_id5 = str(uuid.uuid4())[:8]
        self.client2 = self.create_client(
            name="Another Client",
            email=f"client2_query_{unique_id5}@test.com",
            tenant=self.tenant,
        )

        # Create reviews
        now = timezone.now()
        self.review1 = AmbassadorReview.objects.create(
            ambassador=self.ambassador,
            client=self.client,
            tenant=self.tenant,
            review="Great ambassador!",
            score=5,
            created_by=self.client_user,
            updated_by=self.client_user,
            created_at=now - timedelta(days=5),
        )

        self.review2 = AmbassadorReview.objects.create(
            ambassador=self.ambassador,
            client=self.client2,
            tenant=self.tenant,
            review="Very professional",
            score=4,
            created_by=self.client_user,
            updated_by=self.client_user,
            created_at=now - timedelta(days=3),
        )

        self.review3 = AmbassadorReview.objects.create(
            ambassador=self.ambassador2,
            client=self.client,
            tenant=self.tenant,
            review="Excellent work",
            score=5,
            created_by=self.client_user,
            updated_by=self.client_user,
            created_at=now - timedelta(days=1),
        )

        self.schema = schema_spark
        self.endpoint_path_spark = "/api/v1/graphql/spark"
        self.endpoint_path_client = "/api/v1/graphql/clients"
        self.endpoint_path_mobile = "/api/v1/graphql/mobile"

        self.list_query = """
            query AmbassadorReviews($first: Int, $filters: AmbassadorReviewFiltersInput) {
                ambassadorReviews(first: $first, filters: $filters) {
                    edges {
                        node {
                            id
                            review
                            score
                            ambassadorId
                            clientId
                            tenantId
                        }
                    }
                    totalCount
                    pageInfo {
                        hasNextPage
                        hasPreviousPage
                    }
                }
            }
        """

        self.single_query = """
            query AmbassadorReview($reviewId: ID!) {
                ambassadorReview(reviewId: $reviewId) {
                    id
                    review
                    score
                    ambassadorId
                    clientId
                    tenantId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_ambassador_reviews_list_success(self):
        """Test successful listing of reviews."""
        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReviews"]["totalCount"] >= 3

        edges = result.data["ambassadorReviews"]["edges"]
        assert len(edges) >= 3

        # Verify reviews are ordered by created_at descending (newest first)
        review_ids = [edge["node"]["id"] for edge in edges]
        assert str(self.review3.id) == review_ids[0]  # Most recent

    @pytest.mark.asyncio
    async def test_ambassador_reviews_filter_by_ambassador(self):
        """Test filtering reviews by ambassador."""
        variables = {
            "first": 10,
            "filters": {
                "ambassadorId": str(self.ambassador.id)
            }
        }

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReviews"]["totalCount"] == 2

        edges = result.data["ambassadorReviews"]["edges"]
        for edge in edges:
            assert edge["node"]["ambassadorId"] == str(self.ambassador.id)

    @pytest.mark.asyncio
    async def test_ambassador_reviews_filter_by_client(self):
        """Test filtering reviews by client."""
        variables = {
            "first": 10,
            "filters": {
                "clientId": str(self.client.id)
            }
        }

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReviews"]["totalCount"] == 2

        edges = result.data["ambassadorReviews"]["edges"]
        for edge in edges:
            assert edge["node"]["clientId"] == str(self.client.id)

    @pytest.mark.asyncio
    async def test_ambassador_reviews_filter_by_score_range(self):
        """Test filtering reviews by score range."""
        variables = {
            "first": 10,
            "filters": {
                "minScore": 5,
                "maxScore": 5
            }
        }

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReviews"]["totalCount"] == 2

        edges = result.data["ambassadorReviews"]["edges"]
        for edge in edges:
            assert edge["node"]["score"] == 5

    @pytest.mark.asyncio
    async def test_ambassador_reviews_filter_by_search(self):
        """Test filtering reviews by search text."""
        variables = {
            "first": 10,
            "filters": {
                "search": "professional"
            }
        }

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReviews"]["totalCount"] == 1
        assert "professional" in result.data["ambassadorReviews"]["edges"][0]["node"]["review"].lower(
        )

    @pytest.mark.asyncio
    async def test_ambassador_reviews_filter_by_date_range(self):
        """Test filtering reviews by date range."""
        # Get the actual created_at dates from the reviews
        @sync_to_async
        def get_review_dates():
            review2 = AmbassadorReview.objects.get(pk=self.review2.id)
            review3 = AmbassadorReview.objects.get(pk=self.review3.id)
            return review2.created_at, review3.created_at

        review2_date, review3_date = await get_review_dates()

        # Use dates that include both reviews (slightly before review2, slightly after review3)
        start_date = (review2_date - timedelta(hours=1)
                      ).replace(microsecond=0).isoformat()
        end_date = (review3_date + timedelta(hours=1)
                    ).replace(microsecond=0).isoformat()

        variables = {
            "first": 10,
            "filters": {
                "startDate": start_date,
                "endDate": end_date
            }
        }

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        # Should include review2 and review3
        assert result.data["ambassadorReviews"]["totalCount"] >= 2

    @pytest.mark.asyncio
    async def test_ambassador_reviews_tenant_scoping_for_client(self):
        """Test that client users only see reviews from their tenant."""
        # Create another tenant with a review
        @sync_to_async
        def create_other_tenant_and_review():
            other_tenant = self.create_tenant(name="Other Tenant")
            other_client = self.create_client(
                name="Other Client",
                email=f"other_client_{str(uuid.uuid4())[:8]}@test.com",
                tenant=other_tenant,
            )
            AmbassadorReview.objects.create(
                ambassador=self.ambassador,
                client=other_client,
                tenant=other_tenant,
                review="Other tenant review",
                score=5,
                created_by=self.client_user,
                updated_by=self.client_user,
            )
            return other_tenant
        await create_other_tenant_and_review()

        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None

        # Should only see reviews from client's tenant
        edges = result.data["ambassadorReviews"]["edges"]
        for edge in edges:
            assert edge["node"]["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_ambassador_review_single_success(self):
        """Test successful retrieval of single review."""
        variables = {"reviewId": str(self.review1.id)}

        result = await self._execute_query_authenticated(
            self.single_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReview"] is not None
        assert result.data["ambassadorReview"]["id"] == str(self.review1.id)
        assert result.data["ambassadorReview"]["review"] == "Great ambassador!"
        assert result.data["ambassadorReview"]["score"] == 5

    @pytest.mark.asyncio
    async def test_ambassador_review_single_not_found(self):
        """Test retrieval of non-existent review."""
        variables = {"reviewId": "99999"}

        result = await self._execute_query_authenticated(
            self.single_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReview"] is None

    @pytest.mark.asyncio
    async def test_ambassador_reviews_accessible_by_ambassador(self):
        """Test that ambassadors can query reviews."""
        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.ambassador_user, self.endpoint_path_mobile
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorReviews"] is not None

    @pytest.mark.asyncio
    async def test_ambassador_reviews_spark_admin_all_tenants(self):
        """Test that spark admin can see reviews from all tenants when using spark endpoint."""
        # Create another tenant with a review
        @sync_to_async
        def create_other_tenant_and_review():
            other_tenant = self.create_tenant(name="Other Tenant Spark")
            other_client = self.create_client(
                name="Other Client Spark",
                email=f"other_client_spark_{str(uuid.uuid4())[:8]}@test.com",
                tenant=other_tenant,
            )
            AmbassadorReview.objects.create(
                ambassador=self.ambassador,
                client=other_client,
                tenant=other_tenant,
                review="Other tenant review spark",
                score=5,
                created_by=self.spark_admin_user,
                updated_by=self.spark_admin_user,
            )
            return other_tenant
        await create_other_tenant_and_review()

        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.spark_admin_user, self.endpoint_path_spark
        )

        assert result.errors is None
        assert result.data is not None
        # Should see reviews from all tenants
        assert result.data["ambassadorReviews"]["totalCount"] >= 4
