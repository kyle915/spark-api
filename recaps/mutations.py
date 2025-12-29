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
from events.models import Event, Retailer
from jobs.models import Job
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

        job = None
        if self.input.job_id:
            try:
                job = await sync_to_async(Job.objects.get)(id=self.input.job_id)
            except Job.DoesNotExist:
                raise GraphQLError("Job not found.")

        retailer = None
        if self.input.retailer_id:
            try:
                retailer = await sync_to_async(Retailer.objects.get)(id=self.input.retailer_id)
            except Retailer.DoesNotExist:
                raise GraphQLError("Retailer not found.")

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

                # Create the Recap instance
                total_engagements = 0
                if self.input.consumer_engagements:
                    total_engagements = self.input.consumer_engagements.total_consumer

                recap = models.Recap(
                    name=self.input.name,
                    event=event,
                    created_by=self.user,
                    total_engagements=total_engagements,
                    products_sold=self.input.products_sold,
                    total_earnings=self.input.total_earnings,
                    job=job,
                    retailer=retailer,
                )
                recap.save()

                # Link recap to all recap files
                models.RecapFile.objects.filter(
                    id__in=[recap_file.id for recap_file in recap_files]
                ).update(recap=recap)

                # Create related objects
                if self.input.consumer_engagements:
                    models.ConsumerEngagements.objects.create(
                        recap=recap,
                        created_by=self.user,
                        total_consumer=self.input.consumer_engagements.total_consumer,
                        first_time_consumers=self.input.consumer_engagements.first_time_consumers,
                        brand_aware_consumers=self.input.consumer_engagements.brand_aware_consumers,
                        willing_to_purchase_consumers=self.input.consumer_engagements.willing_to_purchase_consumers,
                        not_willing_consumers=self.input.consumer_engagements.not_willing_consumers,
                    )

                if self.input.product_samples:
                    for sample in self.input.product_samples:
                        models.ProductSamples.objects.create(
                            recap=recap,
                            created_by=self.user,
                            product_id=sample.product_id,
                            quantity=sample.quantity,
                        )

                if self.input.sales_performance:
                    for sale in self.input.sales_performance:
                        models.SalesPerformance.objects.create(
                            recap=recap,
                            created_by=self.user,
                            product_id=sale.product_id,
                            type_of_good_id=sale.type_of_good_id,
                            price=sale.price,
                        )

                if self.input.consumer_feedback:
                    models.ConsumerFeedback.objects.create(
                        recap=recap,
                        created_by=self.user,
                        demographics=self.input.consumer_feedback.demographics,
                        feedback=self.input.consumer_feedback.feedback,
                        quotes=self.input.consumer_feedback.quotes,
                        positive_stories=self.input.consumer_feedback.positive_stories,
                        reasons_to_decline=self.input.consumer_feedback.reasons_to_decline,
                    )

                if self.input.account_feedback:
                    models.AccountFeedback.objects.create(
                        recap=recap,
                        created_by=self.user,
                        do_differently_feedback=self.input.account_feedback.do_differently_feedback,
                        feedback=self.input.account_feedback.feedback,
                        corpo_card=self.input.account_feedback.corpo_card,
                    )
                
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
                    models.RecapFile.objects.filter(recap=recap).distinct()
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
                        recap=recap,
                        approved=False,
                        created_by=self.user,
                    )
                    recap_file.save()
                    final_files.append(recap_file)

                removed_files = list(blob_to_file.values())

                # Update the recap
                recap.name = self.input.name
                recap.event = event
                recap.updated_by = self.user
                recap.save()

                for recap_file in final_files:
                    if recap_file.recap_id != recap.id:
                        recap_file.recap = recap
                        recap_file.save(update_fields=["recap"])

                removed_blob_names = [
                    extract_blob_name_from_url(str(file.file)) for file in removed_files
                ]

                if removed_files:
                    models.RecapFile.objects.filter(
                        id__in=[file.id for file in removed_files]
                    ).delete()

                return recap, removed_blob_names
        
        recap, removed_blob_names = await update_recap_with_files()

        for blob_name in removed_blob_names:
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
                # Detach recap files before deleting recap
                models.RecapFile.objects.filter(recap=recap).update(recap=None)
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
                if recap_file.recap_id:
                    raise GraphQLError(
                        "Recap file is linked to a recap. Update the recap before deleting this file."
                    )

                blob_name = extract_blob_name_from_url(str(recap_file.file))
                recap_file.delete()
                return blob_name

        blob_name = await delete_file_with_references()
        if blob_name:
            delete_blob(blob_name)
        return True

    async def approve_recap(self) -> models.Recap:
        """Approve or decline a recap."""
        if not isinstance(self.input, inputs.ApproveRecapInput):
            raise GraphQLError("Invalid input type.")

        try:
            recap = await sync_to_async(models.Recap.objects.get)(id=self.input.id)
        except models.Recap.DoesNotExist:
            raise GraphQLError("Recap not found.")

        @sync_to_async
        def approve_recap_transaction():
            with transaction.atomic():
                recap.approved = self.input.approved
                recap.updated_by = self.user
                recap.save()
                return recap

        return await approve_recap_transaction()


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

    @relay.mutation(permission_classes=[StrictIsAuthenticated])
    async def approve_recap(
        self,
        info: strawberry.Info,
        input: inputs.ApproveRecapInput,
    ) -> types.RecapDetailResponse:
        """Approve or decline a recap."""
        try:
            service = RecapMutationService.with_input(input)
            await service.set_user(info)
            recap = await service.approve_recap()
            message = "Recap approved successfully." if input.approved else "Recap declined successfully."
            return build_mutation_response(
                types.RecapDetailResponse,
                success=True,
                message=message,
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
