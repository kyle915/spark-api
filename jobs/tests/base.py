"""
Base test class for jobs app tests.

Extends BaseGraphQLTestCase with job-specific helper methods for creating
test data (companies, jobs, rates, etc.).
"""
from django.contrib.auth import get_user_model
from tenants.tests.base import BaseGraphQLTestCase
from jobs import models
from events.models import Event, Location
from ambassadors.models import Ambassador, FileType
from tenants.models import Tenant

User = get_user_model()


class JobsGraphQLTestCase(BaseGraphQLTestCase):
    """
    Extended base test class with job-specific helper methods.

    Inherits all methods from BaseGraphQLTestCase and adds methods for
    creating job-related models.
    """

    def create_status(self, name: str, tenant: Tenant, **kwargs):
        """
        Create a Status instance.

        Args:
            name: Name of the status
            tenant: Tenant instance
            **kwargs: Additional fields to set on the status

        Returns:
            Status: The created status instance
        """
        system_user = self.get_system_user()

        status = models.Status.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return status

    def create_file_type(self, name: str, extension: str | None = None, **kwargs):
        """
        Create a FileType instance.

        Args:
            name: Name of the file type
            extension: File extension (optional)
            **kwargs: Additional fields to set on the file type

        Returns:
            FileType: The created file type instance
        """
        system_user = self.get_system_user()

        file_type = FileType.objects.create(
            name=name,
            extension=extension,
            created_by=system_user,
            **kwargs
        )

        return file_type

    def create_company_file(self, name: str, file_type: FileType, url: str | None = None, **kwargs):
        """
        Create a CompanyFile instance.

        Args:
            name: Name of the company file
            file_type: FileType instance
            url: URL of the file (optional)
            **kwargs: Additional fields to set on the company file

        Returns:
            CompanyFile: The created company file instance
        """
        system_user = self.get_system_user()

        company_file = models.CompanyFile.objects.create(
            name=name,
            file_type=file_type,
            url=url,
            created_by=system_user,
            **kwargs
        )

        return company_file

    def create_company(
        self,
        name: str,
        email: str,
        phone: str,
        tenant: Tenant,
        **kwargs
    ):
        """
        Create a Company instance.

        Args:
            name: Name of the company
            email: Email of the company
            phone: Phone number of the company
            tenant: Tenant instance
            **kwargs: Additional fields to set on the company

        Returns:
            Company: The created company instance
        """
        system_user = self.get_system_user()

        company = models.Company.objects.create(
            name=name,
            email=email,
            phone=phone,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return company

    def create_location(self, name: str, code: str, zip_code: str, tenant: Tenant, **kwargs):
        """
        Create a Location instance.

        Args:
            name: Name of the location
            code: Code of the location
            zip_code: ZIP code
            tenant: Tenant instance
            **kwargs: Additional fields to set on the location

        Returns:
            Location: The created location instance
        """
        system_user = self.get_system_user()

        location = Location.objects.create(
            name=name,
            code=code,
            zip=zip_code,
            created_by=system_user,
            **kwargs
        )

        return location

    def create_event(self, name: str, tenant: Tenant, address: str = "", **kwargs):
        """
        Create an Event instance.

        Args:
            name: Name of the event
            tenant: Tenant instance
            address: Address of the event (default: empty string)
            **kwargs: Additional fields to set on the event

        Returns:
            Event: The created event instance
        """
        system_user = self.get_system_user()

        event = Event.objects.create(
            name=name,
            tenant=tenant,
            address=address,
            created_by=system_user,
            **kwargs
        )

        return event

    def create_ambassador(self, user, **kwargs):
        """
        Create an Ambassador instance.

        Args:
            user: User instance (required)
            **kwargs: Additional fields to set on the ambassador

        Returns:
            Ambassador: The created ambassador instance
        """
        system_user = self.get_system_user()

        ambassador = Ambassador.objects.create(
            user=user,
            created_by=system_user,
            **kwargs
        )

        return ambassador

    def create_ambassador_job(
        self,
        ambassador: Ambassador,
        job: models.Job,
        status: models.Status,
        rate: models.Rate,
        tenant: Tenant,
        **kwargs
    ):
        """
        Create an AmbassadorJob instance.

        Args:
            ambassador: Ambassador instance
            job: Job instance
            status: Status instance
            rate: Rate instance
            tenant: Tenant instance
            **kwargs: Additional fields to set on the ambassador job

        Returns:
            AmbassadorJob: The created ambassador job instance
        """
        system_user = self.get_system_user()

        ambassador_job = models.AmbassadorJob.objects.create(
            ambassador=ambassador,
            job=job,
            status=status,
            rate=rate,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return ambassador_job

    def create_job_title(self, name: str, tenant: Tenant, **kwargs):
        """
        Create a JobTitle instance.

        Args:
            name: Name of the job title
            tenant: Tenant instance
            **kwargs: Additional fields to set on the job title

        Returns:
            JobTitle: The created job title instance
        """
        system_user = self.get_system_user()

        job_title = models.JobTitle.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return job_title

    def create_rate_type(self, name: str, tenant: Tenant, **kwargs):
        """
        Create a RateType instance.

        Args:
            name: Name of the rate type
            tenant: Tenant instance
            **kwargs: Additional fields to set on the rate type

        Returns:
            RateType: The created rate type instance
        """
        system_user = self.get_system_user()

        rate_type = models.RateType.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return rate_type

    def create_rate(self, amount: float, rate_type: models.RateType, tenant: Tenant, **kwargs):
        """
        Create a Rate instance.

        Args:
            amount: Amount of the rate
            rate_type: RateType instance
            tenant: Tenant instance
            **kwargs: Additional fields to set on the rate

        Returns:
            Rate: The created rate instance
        """
        system_user = self.get_system_user()

        rate = models.Rate.objects.create(
            amount=amount,
            rate_type=rate_type,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return rate

    def create_job(
        self,
        name: str,
        code: str,
        address: str,
        event: Event,
        job_title: models.JobTitle,
        tenant: Tenant,
        **kwargs
    ):
        """
        Create a Job instance.

        Args:
            name: Name of the job
            code: Code of the job
            address: Address of the job
            event: Event instance
            job_title: JobTitle instance
            tenant: Tenant instance
            **kwargs: Additional fields to set on the job (e.g., coordinates, rate)

        Returns:
            Job: The created job instance
        """
        system_user = self.get_system_user()

        job = models.Job.objects.create(
            name=name,
            code=code,
            address=address,
            event=event,
            job_title=job_title,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return job

    def create_job_file(
        self,
        name: str,
        url: str,
        job: models.Job,
        file_type: FileType,
        **kwargs
    ):
        """
        Create a JobFile instance.

        Args:
            name: Name of the job file
            url: URL of the file
            job: Job instance
            file_type: FileType instance
            **kwargs: Additional fields to set on the job file

        Returns:
            JobFile: The created job file instance
        """
        system_user = self.get_system_user()

        job_file = models.JobFile.objects.create(
            name=name,
            url=url,
            job=job,
            file_type=file_type,
            created_by=system_user,
            **kwargs
        )

        return job_file

    def create_job_requirement_type(self, name: str, tenant: Tenant, **kwargs):
        """
        Create a JobRequirementType instance.

        Args:
            name: Name of the job requirement type
            tenant: Tenant instance
            **kwargs: Additional fields to set on the job requirement type

        Returns:
            JobRequirementType: The created job requirement type instance
        """
        system_user = self.get_system_user()

        job_requirement_type = models.JobRequirementType.objects.create(
            name=name,
            tenant=tenant,
            created_by=system_user,
            **kwargs
        )

        return job_requirement_type

    def create_job_requirement(
        self,
        name: str,
        job: models.Job,
        job_requirement_type: models.JobRequirementType,
        **kwargs
    ):
        """
        Create a JobRequirement instance.

        Args:
            name: Name of the job requirement
            job: Job instance (tenant is derived from job)
            job_requirement_type: JobRequirementType instance
            **kwargs: Additional fields to set on the job requirement

        Returns:
            JobRequirement: The created job requirement instance
        """
        system_user = self.get_system_user()

        job_requirement = models.JobRequirement.objects.create(
            name=name,
            job=job,
            job_requirement_type=job_requirement_type,
            tenant=job.tenant,  # Get tenant from job
            created_by=system_user,
            **kwargs
        )

        return job_requirement

    async def _execute_mutation_authenticated(
        self,
        mutation,
        variables,
        user: User,
        endpoint_path=None
    ):
        """
        Helper method to execute GraphQL mutations with an authenticated user.

        This method extends _execute_mutation to support authenticated requests
        by setting the user in the request context.

        Args:
            mutation: GraphQL mutation string
            variables: Variables dictionary
            user: Authenticated user instance
            endpoint_path: The actual endpoint path being tested (optional,
                          defaults to self.endpoint_path if set)

        Returns:
            ExecutionResult: The result from schema.execute()
        """
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        # Use endpoint_path from parameter or fall back to instance attribute
        path = endpoint_path or getattr(
            self, 'endpoint_path', '/api/v1/graphql')

        factory = RequestFactory()
        wsgi_request = factory.post(path)
        wsgi_request.user = user  # Set authenticated user

        # Create a mock ASGI request object that JwtSchema expects
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
                self.scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    USER_OR_ERROR_KEY: MockUserOrError(wsgi_request.user),
                }
                # Also set USER_OR_ERROR_KEY as an attribute (middleware expects this)
                setattr(self, USER_OR_ERROR_KEY,
                        MockUserOrError(wsgi_request.user))

        mock_request = MockASGIRequest(wsgi_request, path)

        # Create a context object that has a 'request' attribute
        # This is what the permission classes expect (info.context.request)
        # It also needs to be subscriptable for extensions (context["request"])
        class Context:
            def __init__(self, request):
                self.request = request

            def __getitem__(self, key):
                if key == "request":
                    return self.request
                raise KeyError(key)

        context = Context(mock_request)

        # Use execute (async) since mutations are async
        result = await self.schema.execute(
            mutation,
            variable_values=variables,
            context_value=context,
        )
        return result

    async def _execute_query_authenticated(
        self,
        query,
        variables=None,
        user: User = None,
        endpoint_path=None
    ):
        """
        Helper method to execute GraphQL queries with an authenticated user.

        Args:
            query: GraphQL query string
            variables: Variables dictionary (optional)
            user: Authenticated user instance
            endpoint_path: The actual endpoint path being tested (optional,
                          defaults to self.endpoint_path if set)

        Returns:
            ExecutionResult: The result from schema.execute()
        """
        from django.test import RequestFactory
        from gqlauth.core.middlewares import USER_OR_ERROR_KEY

        # Use endpoint_path from parameter or fall back to instance attribute
        path = endpoint_path or getattr(
            self, 'endpoint_path', '/api/v1/graphql')

        factory = RequestFactory()
        wsgi_request = factory.post(path)
        wsgi_request.user = user  # Set authenticated user

        # Create a mock ASGI request object that JwtSchema expects
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
                self.scope = {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    USER_OR_ERROR_KEY: MockUserOrError(wsgi_request.user),
                }
                # Also set USER_OR_ERROR_KEY as an attribute (middleware expects this)
                setattr(self, USER_OR_ERROR_KEY,
                        MockUserOrError(wsgi_request.user))

        mock_request = MockASGIRequest(wsgi_request, path)

        # Create a context object that has a 'request' attribute
        # This is what the permission classes expect (info.context.request)
        # It also needs to be subscriptable for extensions (context["request"])
        class Context:
            def __init__(self, request):
                self.request = request

            def __getitem__(self, key):
                if key == "request":
                    return self.request
                raise KeyError(key)

        context = Context(mock_request)

        # Use execute (async) since queries are async
        result = await self.schema.execute(
            query,
            variable_values=variables or {},
            context_value=context,
        )
        return result
