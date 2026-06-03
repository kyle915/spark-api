"""
Tests for ambassador note mutations and queries.

This module tests:
- create_ambassador_note mutation (authenticated users only)
- update_ambassador_note mutation (authenticated users only)
- delete_ambassador_note mutation (authenticated users only)
- ambassador_notes query (authenticated users)
- ambassador_note query (authenticated users)
"""
import base64
import pytest
import strawberry_django  # noqa: F401
import uuid
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta, datetime

from ambassadors.models import Ambassador, AmbassadorNote
from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from tenants.models import Tenant, TenantedUser
from utils.utils import ROLE_ID

User = get_user_model()


@pytest.mark.django_db(transaction=True)
class TestCreateAmbassadorNote(AmbassadorsGraphQLTestCase):
    """Tests for create_ambassador_note mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Note Creation Tenant")
        # Use UUID for users to ensure uniqueness across test runs
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_note_{unique_id}@test.com",
            email=f"client_note_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_note_{unique_id2}@test.com",
            email=f"spark_note_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_note_{unique_id3}@test.com",
            email=f"ambassador_note_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation CreateAmbassadorNote($input: CreateAmbassadorNoteInput!) {
                createAmbassadorNote(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorNote {
                        id
                        note
                        ambassadorId
                        tenantId
                        createdById
                        updatedById
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_create_ambassador_note_success_by_client(self):
        """Test successful note creation by client."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "note": "Great ambassador, very professional!",
                "clientMutationId": "test-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorNote"]["success"] is True
        assert "created successfully" in result.data["createAmbassadorNote"]["message"].lower(
        )
        assert result.data["createAmbassadorNote"]["clientMutationId"] == "test-123"
        assert result.data["createAmbassadorNote"]["ambassadorNote"]["note"] == "Great ambassador, very professional!"

        # Verify in DB
        @sync_to_async
        def get_note():
            return AmbassadorNote.objects.select_related('tenant', 'created_by').get(
                ambassador=self.ambassador,
                tenant=self.tenant
            )
        note = await get_note()
        assert note.note == "Great ambassador, very professional!"
        assert note.tenant == self.tenant
        assert note.created_by == self.client_user

    @pytest.mark.asyncio
    async def test_create_ambassador_note_success_by_spark_admin(self):
        """Test successful note creation by spark admin."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "note": "Excellent work!",
                "tenantId": str(self.tenant.id),
                "clientMutationId": "test-456",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorNote"]["success"] is True

    @pytest.mark.asyncio
    async def test_create_ambassador_note_success_by_ambassador(self):
        """Test successful note creation by ambassador (any authenticated user can create)."""
        # Create another ambassador to note about
        unique_id = str(uuid.uuid4())[:8]
        ambassador_user2 = await sync_to_async(self.create_user)(
            username=f"ambassador2_note_{unique_id}@test.com",
            email=f"ambassador2_note_{unique_id}@test.com",
            role=self.roles['ambassador']
        )
        await sync_to_async(self.create_tenanted_user)(ambassador_user2, self.tenant)
        ambassador2 = await sync_to_async(self.create_ambassador)(
            ambassador_user2,
            address="456 Test Ave",
            is_active=True,
        )

        variables = {
            "input": {
                "ambassadorId": str(ambassador2.id),
                "note": "Note from another ambassador",
                "clientMutationId": "test-ambassador",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["createAmbassadorNote"]["success"] is True

    @pytest.mark.asyncio
    async def test_create_ambassador_note_ambassador_not_found(self):
        """Test creating note for non-existent ambassador."""
        variables = {
            "input": {
                "ambassadorId": "99999",
                "note": "Test note",
                "clientMutationId": "test-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["createAmbassadorNote"]["success"] is False
        assert "not found" in result.data["createAmbassadorNote"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_create_ambassador_note_empty_note(self):
        """Test creating note with empty string (model allows empty strings)."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "note": "",
                "clientMutationId": "test-empty",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        # Empty note is allowed by the model (TextField with null=False but no blank=False)
        assert result.data is not None
        assert result.data["createAmbassadorNote"]["success"] is True
        assert result.data["createAmbassadorNote"]["ambassadorNote"]["note"] == ""

    @pytest.mark.asyncio
    async def test_create_ambassador_note_unauthorized(self):
        """Test creating note by unauthenticated user."""
        variables = {
            "input": {
                "ambassadorId": str(self.ambassador.id),
                "note": "Unauthorized note",
                "clientMutationId": "test-unauthorized",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path, user=None
        )

        # Should be rejected at GraphQL level by StrictIsAuthenticated
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestUpdateAmbassadorNote(AmbassadorsGraphQLTestCase):
    """Tests for update_ambassador_note mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Note Update Tenant")
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_update_note_{unique_id}@test.com",
            email=f"client_update_note_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_update_note_{unique_id2}@test.com",
            email=f"spark_update_note_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_update_note_{unique_id3}@test.com",
            email=f"ambassador_update_note_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        # Create existing note
        self.note = AmbassadorNote.objects.create(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Original note",
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation UpdateAmbassadorNote($input: UpdateAmbassadorNoteInput!) {
                updateAmbassadorNote(input: $input) {
                    success
                    message
                    clientMutationId
                    ambassadorNote {
                        id
                        note
                    }
                }
            }
        """

    @pytest.mark.asyncio
    async def test_update_ambassador_note_success_by_client(self):
        """Test successful note update by client."""
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "note": "Updated note text",
                "clientMutationId": "test-update-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["updateAmbassadorNote"]["success"] is True
        assert "updated successfully" in result.data["updateAmbassadorNote"]["message"].lower(
        )
        assert result.data["updateAmbassadorNote"]["ambassadorNote"]["note"] == "Updated note text"

        # Verify in DB
        @sync_to_async
        def get_note():
            return AmbassadorNote.objects.select_related('updated_by').get(pk=self.note.id)
        note = await get_note()
        assert note.note == "Updated note text"
        assert note.updated_by == self.client_user

    @pytest.mark.asyncio
    async def test_update_ambassador_note_success_by_spark_admin(self):
        """Test successful note update by spark admin."""
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "note": "Spark admin updated note",
                "clientMutationId": "test-update-spark",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorNote"]["success"] is True

        # Verify in DB
        @sync_to_async
        def get_note():
            return AmbassadorNote.objects.select_related('updated_by').get(pk=self.note.id)
        note = await get_note()
        assert note.note == "Spark admin updated note"
        assert note.updated_by == self.spark_admin_user

    @pytest.mark.asyncio
    async def test_update_ambassador_note_success_by_ambassador(self):
        """Test successful note update by ambassador."""
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "note": "Ambassador updated note",
                "clientMutationId": "test-update-ambassador",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorNote"]["success"] is True

        # Verify in DB
        @sync_to_async
        def get_note():
            return AmbassadorNote.objects.select_related('updated_by').get(pk=self.note.id)
        note = await get_note()
        assert note.note == "Ambassador updated note"
        assert note.updated_by == self.ambassador_user

    @pytest.mark.asyncio
    async def test_update_ambassador_note_no_changes(self):
        """Test updating note without providing new note (should keep original)."""
        original_note = self.note.note
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "clientMutationId": "test-no-changes",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorNote"]["success"] is True

        # Verify note unchanged
        @sync_to_async
        def get_note():
            return AmbassadorNote.objects.get(pk=self.note.id)
        note = await get_note()
        assert note.note == original_note

    @pytest.mark.asyncio
    async def test_update_ambassador_note_not_found(self):
        """Test updating non-existent note."""
        variables = {
            "input": {
                "noteId": "99999",
                "note": "Updated note",
                "clientMutationId": "test-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["updateAmbassadorNote"]["success"] is False
        assert "not found" in result.data["updateAmbassadorNote"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_update_ambassador_note_unauthorized(self):
        """Test updating note by unauthenticated user."""
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "note": "Unauthorized update",
                "clientMutationId": "test-unauthorized",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path, user=None
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestDeleteAmbassadorNote(AmbassadorsGraphQLTestCase):
    """Tests for delete_ambassador_note mutation."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Note Delete Tenant")
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_delete_note_{unique_id}@test.com",
            email=f"client_delete_note_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_delete_note_{unique_id2}@test.com",
            email=f"spark_delete_note_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_delete_note_{unique_id3}@test.com",
            email=f"ambassador_delete_note_{unique_id3}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user, self.tenant)
        self.ambassador = self.create_ambassador(
            self.ambassador_user,
            address="123 Test St",
            coordinates=[40.7128, -74.0060],
            is_active=True,
        )

        # Create note to delete
        self.note = AmbassadorNote.objects.create(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Note to delete",
            created_by=self.client_user,
            updated_by=self.client_user,
        )

        self.schema = schema_spark
        self.endpoint_path = "/api/v1/graphql/spark"

        self.mutation = """
            mutation DeleteAmbassadorNote($input: DeleteAmbassadorNoteInput!) {
                deleteAmbassadorNote(input: $input) {
                    success
                    message
                    clientMutationId
                }
            }
        """

    @pytest.mark.asyncio
    async def test_delete_ambassador_note_success_by_client(self):
        """Test successful note deletion by client."""
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "clientMutationId": "test-delete-123",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["deleteAmbassadorNote"]["success"] is True
        assert "deleted successfully" in result.data["deleteAmbassadorNote"]["message"].lower(
        )

        # Verify note was deleted
        note_exists = await sync_to_async(
            AmbassadorNote.objects.filter(pk=self.note.id).exists
        )()
        assert note_exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_note_success_by_spark_admin(self):
        """Test successful note deletion by spark admin."""
        # Create another note for deletion
        note2 = await sync_to_async(AmbassadorNote.objects.create)(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Another note",
            created_by=self.spark_admin_user,
            updated_by=self.spark_admin_user,
        )

        variables = {
            "input": {
                "noteId": str(note2.id),
                "clientMutationId": "test-delete-spark",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.spark_admin_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteAmbassadorNote"]["success"] is True

        # Verify note was deleted
        note_exists = await sync_to_async(
            AmbassadorNote.objects.filter(pk=note2.id).exists
        )()
        assert note_exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_note_success_by_ambassador(self):
        """Test successful note deletion by ambassador."""
        # Create another note for deletion
        note2 = await sync_to_async(AmbassadorNote.objects.create)(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Note to delete by ambassador",
            created_by=self.ambassador_user,
            updated_by=self.ambassador_user,
        )

        variables = {
            "input": {
                "noteId": str(note2.id),
                "clientMutationId": "test-delete-ambassador",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.ambassador_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteAmbassadorNote"]["success"] is True

        # Verify note was deleted
        note_exists = await sync_to_async(
            AmbassadorNote.objects.filter(pk=note2.id).exists
        )()
        assert note_exists is False

    @pytest.mark.asyncio
    async def test_delete_ambassador_note_not_found(self):
        """Test deletion of non-existent note."""
        variables = {
            "input": {
                "noteId": "99999",
                "clientMutationId": "test-not-found",
            }
        }

        result = await self._execute_mutation_authenticated(
            self.mutation, variables, self.client_user, self.endpoint_path
        )

        assert result.data is not None
        assert result.data["deleteAmbassadorNote"]["success"] is False
        assert "not found" in result.data["deleteAmbassadorNote"]["message"].lower(
        )

    @pytest.mark.asyncio
    async def test_delete_ambassador_note_unauthorized(self):
        """Test note deletion by unauthenticated user."""
        variables = {
            "input": {
                "noteId": str(self.note.id),
                "clientMutationId": "test-unauthorized",
            }
        }

        result = await self._execute_mutation(
            self.mutation, variables, self.endpoint_path, user=None
        )

        # Permission class rejects at GraphQL level
        assert result.data is None
        assert result.errors is not None
        assert len(result.errors) > 0


@pytest.mark.django_db(transaction=True)
class TestAmbassadorNoteQueries(AmbassadorsGraphQLTestCase):
    """Tests for ambassador note queries."""

    @pytest.fixture(autouse=True)
    def setup(self, db):
        """Set up test data."""
        from config.schema_spark import schema_spark
        from config.schema_client import schema_clients
        from config.schema_mobile import schema_mobile

        self.roles = self.setup_default_roles()
        self.tenant = self.create_tenant(name="Note Query Tenant")
        unique_id = str(uuid.uuid4())[:8]
        self.client_user = self.create_user(
            username=f"client_query_note_{unique_id}@test.com",
            email=f"client_query_note_{unique_id}@test.com",
            role=self.roles['client']
        )
        self.create_tenanted_user(self.client_user, self.tenant)

        unique_id2 = str(uuid.uuid4())[:8]
        self.spark_admin_user = self.create_user(
            username=f"spark_query_note_{unique_id2}@test.com",
            email=f"spark_query_note_{unique_id2}@test.com",
            role=self.roles['spark_admin']
        )

        unique_id3 = str(uuid.uuid4())[:8]
        self.ambassador_user = self.create_user(
            username=f"ambassador_query_note_{unique_id3}@test.com",
            email=f"ambassador_query_note_{unique_id3}@test.com",
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
            username=f"ambassador2_query_note_{unique_id4}@test.com",
            email=f"ambassador2_query_note_{unique_id4}@test.com",
            role=self.roles['ambassador'],
        )
        self.create_tenanted_user(self.ambassador_user2, self.tenant)
        self.ambassador2 = self.create_ambassador(
            self.ambassador_user2,
            address="456 Test Ave",
            is_active=True,
        )

        # Create notes
        now = timezone.now()
        self.note1 = AmbassadorNote.objects.create(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Great ambassador note!",
            created_by=self.client_user,
            updated_by=self.client_user,
            created_at=now - timedelta(days=5),
        )

        self.note2 = AmbassadorNote.objects.create(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Very professional note",
            created_by=self.client_user,
            updated_by=self.client_user,
            created_at=now - timedelta(days=3),
        )

        self.note3 = AmbassadorNote.objects.create(
            ambassador=self.ambassador2,
            tenant=self.tenant,
            note="Excellent work note",
            created_by=self.client_user,
            updated_by=self.client_user,
            created_at=now - timedelta(days=1),
        )

        self.note4 = AmbassadorNote.objects.create(
            ambassador=self.ambassador,
            tenant=self.tenant,
            note="Another note by spark admin",
            created_by=self.spark_admin_user,
            updated_by=self.spark_admin_user,
            created_at=now - timedelta(hours=12),
        )

        self.schema = schema_spark
        self.endpoint_path_spark = "/api/v1/graphql/spark"
        self.endpoint_path_client = "/api/v1/graphql/clients"
        self.endpoint_path_mobile = "/api/v1/graphql/mobile"

        self.list_query = """
            query AmbassadorNotes($first: Int, $filters: AmbassadorNoteFiltersInput) {
                ambassadorNotes(first: $first, filters: $filters) {
                    edges {
                        node {
                            id
                            note
                            ambassadorId
                            tenantId
                            createdById
                            updatedById
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
            query AmbassadorNote($noteId: ID!) {
                ambassadorNote(noteId: $noteId) {
                    id
                    note
                    ambassadorId
                    tenantId
                    createdById
                    updatedById
                }
            }
        """

    @pytest.mark.asyncio
    async def test_ambassador_notes_list_success(self):
        """Test successful listing of notes."""
        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorNotes"]["totalCount"] >= 4

        edges = result.data["ambassadorNotes"]["edges"]
        assert len(edges) >= 4

        # Verify notes are ordered by created_at descending (newest first).
        # node.id is a Relay global ID (base64 "AmbassadorNoteType:<pk>").
        note_ids = [
            base64.b64decode(edge["node"]["id"]).decode("utf-8")
            for edge in edges
        ]
        assert note_ids[0] == f"AmbassadorNoteType:{self.note4.id}"  # Most recent

    @pytest.mark.asyncio
    async def test_ambassador_notes_filter_by_ambassador(self):
        """Test filtering notes by ambassador."""
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
        assert result.data["ambassadorNotes"]["totalCount"] == 3

        edges = result.data["ambassadorNotes"]["edges"]
        for edge in edges:
            assert edge["node"]["ambassadorId"] == str(self.ambassador.id)

    @pytest.mark.asyncio
    async def test_ambassador_notes_filter_by_created_by(self):
        """Test filtering notes by created_by."""
        variables = {
            "first": 10,
            "filters": {
                "createdById": str(self.spark_admin_user.id)
            }
        }

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorNotes"]["totalCount"] == 1

        edges = result.data["ambassadorNotes"]["edges"]
        assert edges[0]["node"]["createdById"] == str(self.spark_admin_user.id)

    @pytest.mark.asyncio
    async def test_ambassador_notes_filter_by_search(self):
        """Test filtering notes by search text."""
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
        assert result.data["ambassadorNotes"]["totalCount"] == 1
        assert "professional" in result.data["ambassadorNotes"]["edges"][0]["node"]["note"].lower(
        )

    @pytest.mark.asyncio
    async def test_ambassador_notes_filter_by_date_range(self):
        """Test filtering notes by date range."""
        # Get the actual created_at dates from the notes
        @sync_to_async
        def get_note_dates():
            note2 = AmbassadorNote.objects.get(pk=self.note2.id)
            note3 = AmbassadorNote.objects.get(pk=self.note3.id)
            return note2.created_at, note3.created_at

        note2_date, note3_date = await get_note_dates()

        # Use dates that include both notes (slightly before note2, slightly after note3)
        start_date = (note2_date - timedelta(hours=1)
                      ).replace(microsecond=0).isoformat()
        end_date = (note3_date + timedelta(hours=1)
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
        # Should include note2 and note3
        assert result.data["ambassadorNotes"]["totalCount"] >= 2

    @pytest.mark.asyncio
    async def test_ambassador_notes_tenant_scoping_for_client(self):
        """Test that client users only see notes from their tenant."""
        # Create another tenant with a note
        @sync_to_async
        def create_other_tenant_and_note():
            other_tenant = self.create_tenant(name="Other Tenant")
            other_ambassador_user = self.create_user(
                username=f"other_ambassador_{str(uuid.uuid4())[:8]}@test.com",
                email=f"other_ambassador_{str(uuid.uuid4())[:8]}@test.com",
                role=self.roles['ambassador'],
            )
            other_ambassador = self.create_ambassador(
                other_ambassador_user,
                address="789 Other St",
                is_active=True,
            )
            AmbassadorNote.objects.create(
                ambassador=other_ambassador,
                tenant=other_tenant,
                note="Other tenant note",
                created_by=self.client_user,
                updated_by=self.client_user,
            )
            return other_tenant
        await create_other_tenant_and_note()

        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None

        # Should only see notes from client's tenant
        edges = result.data["ambassadorNotes"]["edges"]
        for edge in edges:
            assert edge["node"]["tenantId"] == str(self.tenant.id)

    @pytest.mark.asyncio
    async def test_ambassador_note_single_success(self):
        """Test successful retrieval of single note."""
        variables = {"noteId": str(self.note1.id)}

        result = await self._execute_query_authenticated(
            self.single_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorNote"] is not None
        decoded_id = base64.b64decode(
            result.data["ambassadorNote"]["id"]).decode("utf-8")
        assert decoded_id == f"AmbassadorNoteType:{self.note1.id}"
        assert result.data["ambassadorNote"]["note"] == "Great ambassador note!"

    @pytest.mark.asyncio
    async def test_ambassador_note_single_not_found(self):
        """Test retrieval of non-existent note."""
        variables = {"noteId": "99999"}

        result = await self._execute_query_authenticated(
            self.single_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorNote"] is None

    @pytest.mark.asyncio
    async def test_ambassador_notes_accessible_by_ambassador(self):
        """Test that ambassadors can query notes."""
        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.ambassador_user, self.endpoint_path_mobile
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorNotes"] is not None

    @pytest.mark.asyncio
    async def test_ambassador_notes_spark_admin_all_tenants(self):
        """Test that spark admin can see notes from all tenants when using spark endpoint."""
        # Create another tenant with a note
        @sync_to_async
        def create_other_tenant_and_note():
            other_tenant = self.create_tenant(name="Other Tenant Spark")
            other_ambassador_user = self.create_user(
                username=f"other_ambassador_spark_{str(uuid.uuid4())[:8]}@test.com",
                email=f"other_ambassador_spark_{str(uuid.uuid4())[:8]}@test.com",
                role=self.roles['ambassador'],
            )
            other_ambassador = self.create_ambassador(
                other_ambassador_user,
                address="789 Other St Spark",
                is_active=True,
            )
            AmbassadorNote.objects.create(
                ambassador=other_ambassador,
                tenant=other_tenant,
                note="Other tenant note spark",
                created_by=self.spark_admin_user,
                updated_by=self.spark_admin_user,
            )
            return other_tenant
        await create_other_tenant_and_note()

        variables = {"first": 10}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.spark_admin_user, self.endpoint_path_spark
        )

        assert result.errors is None
        assert result.data is not None
        # Should see notes from all tenants
        assert result.data["ambassadorNotes"]["totalCount"] >= 5

    @pytest.mark.asyncio
    async def test_ambassador_notes_pagination(self):
        """Test pagination of notes."""
        variables = {"first": 2}

        result = await self._execute_query_authenticated(
            self.list_query, variables, self.client_user, self.endpoint_path_client
        )

        assert result.errors is None
        assert result.data is not None
        assert result.data["ambassadorNotes"]["totalCount"] >= 4
        assert len(result.data["ambassadorNotes"]["edges"]) == 2
        assert result.data["ambassadorNotes"]["pageInfo"]["hasNextPage"] is True

