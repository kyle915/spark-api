import strawberry
from strawberry import relay
from graphql import GraphQLError
from asgiref.sync import sync_to_async
from typing import Any

from django.contrib.auth import get_user_model
from django.db.models import Model
from django.db import transaction

from recaps import types
from recaps import models
from recaps import inputs
from ambassadors.models import FileType
from events.models import Event
from utils.graphql.inputs import SparkGraphQLInput
from utils.graphql.permissions import StrictIsAuthenticated
from utils.graphql.relay import ensure_relay_mutation
from utils.graphql.mixins import SparkGraphQLMixin
from utils.utils import build_mutation_response
from utils.gcs import extract_blob_name_from_url, delete_blob

ensure_relay_mutation()

User = get_user_model()


class RecapMutationService(SparkGraphQLMixin):
    """Service for recap mutations."""

    input: SparkGraphQLInput | None = None
    info: strawberry.Info | None = None
    user: User | None = None

    @classmethod
    def with_input(cls, input: SparkGraphQLInput) -> "RecapMutationService":
        """Create a new instance of the service with the input."""
        service = cls()
        service.set_input(input)
        return service

    def set_input(self, input: SparkGraphQLInput) -> "RecapMutationService":
        """Set the input for the service."""
        self.input = input
        return self

    async def set_user(self, info: strawberry.Info) -> "RecapMutationService":
        """Set the user for the service."""
        self.info = info
        self.user = await self.get_user(info)
        return self

    async def create_recap(self) -> models.Recap:
        """Create a recap with multiple files."""
        if not isinstance(self.input, inputs.CreateRecapInput):
            raise GraphQLError("Invalid input type.")

        # Validate event exists
        try:
            event = await sync_to_async(Event.objects.get)(id=self.input.event_id)
        except Event.DoesNotExist:
            raise GraphQLError("Event not found.")

        if not self.input.files or len(self.input.files) == 0:
            raise GraphQLError("At least one file is required.")

        # Use transaction to ensure atomicity
        @sync_to_async
        def create_recap_with_files():
            with transaction.atomic():
                # Create RecapFile instances for each file
                recap_files = []
                for file_url in self.input.files:
                    # Extract blob name from GCS URL
                    blob_name = extract_blob_name_from_url(file_url)
                    
                    # Get default file type (you may want to make this configurable)
                    file_type = FileType.objects.first()
                    if not file_type:
                        raise GraphQLError("No file type available. Please create a file type first.")

                    recap_file = models.RecapFile(
                        name=f"Recap file for {self.input.name}",
                        file=blob_name,
                        file_type=file_type,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    recap_files.append(recap_file)

                # Create the Recap instance with the first file as the main file
                recap = models.Recap(
                    name=self.input.name,
                    event=event,
                    recap_file=recap_files[0],
                    created_by=self.user,
                )
                recap.save()

                # Create RecapRecapFile entries for ALL files
                for recap_file in recap_files:
                    recap_recap_file = models.RecapRecapFile(
                        recap=recap,
                        recap_file=recap_file,
                        created_by=self.user,
                    )
                    recap_recap_file.save()
                
                return recap
        
        return await create_recap_with_files()

    async def update_recap(self) -> models.Recap:
        """Update a recap."""
        if not isinstance(self.input, inputs.UpdateRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap = await sync_to_async(models.Recap.objects.get)(id=self.input.id)
        except models.Recap.DoesNotExist:
            raise GraphQLError("Recap not found.")

        # Validate event exists
        try:
            event = await sync_to_async(Event.objects.get)(id=self.input.event_id)
        except Event.DoesNotExist:
            raise GraphQLError("Event not found.")

        if not self.input.files or len(self.input.files) == 0:
            raise GraphQLError("At least one file is required.")

        @sync_to_async
        def update_recap_with_files():
            with transaction.atomic():
                existing_files = list(
                    models.RecapFile.objects.filter(recap_recap_file__recap=recap).distinct()
                )
                blob_to_file = {
                    extract_blob_name_from_url(str(file.file)): file
                    for file in existing_files
                    if extract_blob_name_from_url(str(file.file))
                }

                final_files: list[models.RecapFile] = []
                for file_url in self.input.files:
                    blob_name = extract_blob_name_from_url(file_url)
                    if not blob_name:
                        raise GraphQLError("Invalid recap file path.")

                    if blob_name in blob_to_file:
                        # Reuse existing file; mark as kept by popping
                        final_files.append(blob_to_file.pop(blob_name))
                        continue

                    file_type = FileType.objects.first()
                    if not file_type:
                        raise GraphQLError("No file type available.")

                    recap_file = models.RecapFile(
                        name=f"Recap file for {self.input.name}",
                        file=blob_name,
                        file_type=file_type,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    final_files.append(recap_file)

                removed_files = list(blob_to_file.values())

                # Update the recap
                recap.name = self.input.name
                recap.event = event
                recap.recap_file = final_files[0]
                recap.updated_by = self.user
                recap.save()

                # Remove only relations for files no longer linked
                models.RecapRecapFile.objects.filter(recap=recap).exclude(
                    recap_file__in=final_files
                ).delete()

                # Ensure relations exist for all current files
                existing_relations = set(
                    models.RecapRecapFile.objects.filter(
                        recap=recap, recap_file__in=final_files
                    ).values_list("recap_file_id", flat=True)
                )
                for recap_file in final_files:
                    if recap_file.id not in existing_relations:
                        models.RecapRecapFile.objects.create(
                            recap=recap,
                            recap_file=recap_file,
                            created_by=self.user,
                        )

                removed_blob_names = [
                    extract_blob_name_from_url(str(file.file)) for file in removed_files
                ]

                if removed_files:
                    models.RecapFile.objects.filter(
                        id__in=[file.id for file in removed_files]
                    ).delete()

                return recap, removed_blob_names
        
        recap, existing_blob_names = await update_recap_with_files()

        for blob_name in existing_blob_names:
            if blob_name:
                delete_blob(blob_name)

        return recap

    async def delete_recap(self) -> bool:
        """Delete a recap."""
        if not isinstance(self.input, inputs.DeleteRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap = await sync_to_async(models.Recap.objects.get)(id=self.input.id)
        except models.Recap.DoesNotExist:
            raise GraphQLError("Recap not found.")

        @sync_to_async
        def delete_recap_with_files():
            with transaction.atomic():
                # Delete RecapRecapFile entries
                models.RecapRecapFile.objects.filter(recap=recap).delete()
                # Delete the recap
                recap.delete()
            return True
        
        return await delete_recap_with_files()

    async def delete_recap_file(self) -> bool:
        """Delete a recap file and its blob from GCS."""
        if not isinstance(self.input, inputs.DeleteRecapFileInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap_file = await sync_to_async(models.RecapFile.objects.get)(
                id=self.input.id
            )
        except models.RecapFile.DoesNotExist:
            raise GraphQLError("Recap file not found.")

        @sync_to_async
        def delete_file_with_references():
            with transaction.atomic():
                if models.Recap.objects.filter(recap_file=recap_file).exists():
                    raise GraphQLError(
                        "Recap file is set as primary for a recap. Update the recap before deleting this file."
                    )

                blob_name = extract_blob_name_from_url(str(recap_file.file))
                models.RecapRecapFile.objects.filter(recap_file=recap_file).delete()
                recap_file.delete()
                return blob_name

        blob_name = await delete_file_with_references()
        if blob_name:
            delete_blob(blob_name)
        return True


@strawberry.type
class RecapMutations:
    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def create_recap(
        self,
        info: strawberry.Info,
        input: inputs.CreateRecapInput,
    ) -> types.RecapDetailResponse:
        """Create a new recap with multiple files."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.create_recap()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap created successfully.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def update_recap(
        self,
        info: strawberry.Info,
        input: inputs.UpdateRecapInput,
    ) -> types.RecapDetailResponse:
        """Update an existing recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.update_recap()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap updated successfully.",
                input_obj=input,
                recap=recap,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapInput,
    ) -> types.RecapDetailResponse:
        """Delete a recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            await service.delete_recap()
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message="Recap deleted successfully.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def delete_recap_file(
        self,
        info: strawberry.Info,
        input: inputs.DeleteRecapFileInput,
    ) -> types.RecapFileDetailResponse:
        """Delete a recap file and its blob."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            await service.delete_recap_file()
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=True,
                message="Recap file deleted successfully.",
                input_obj=input,
            )
        except GraphQLError as e:
            return build_mutation_response(
                types.RecapFileDetailResponse,
                success=False,
                message=str(e),
                input_obj=input,
            )
