"""
Tests for direct model creation using helper methods.

This module tests that all helper methods in JobsGraphQLTestCase work correctly
and that model relationships are properly established.
"""
import pytest
from jobs.tests.base import JobsGraphQLTestCase
from jobs import models
from events.models import Event, Location
from ambassadors.models import Ambassador, FileType
from tenants.models import Tenant


@pytest.mark.django_db(transaction=True)
class TestJobsModelCreation(JobsGraphQLTestCase):
    """Tests for direct model creation using helper methods."""

    def test_create_status(self):
        """Test creating a status directly."""
        tenant = self.create_tenant(name="Test Tenant")
        status = self.create_status(name="Test Status", tenant=tenant)

        assert status.id is not None
        assert status.name == "Test Status"
        assert status.tenant == tenant
        assert status.created_by is not None

    def test_create_file_type(self):
        """Test creating a file type directly."""
        file_type = self.create_file_type(name="PDF", extension=".pdf")

        assert file_type.id is not None
        assert file_type.name == "PDF"
        assert file_type.extension == ".pdf"
        assert file_type.created_by is not None

    def test_create_company_file(self):
        """Test creating a company file directly."""
        file_type = self.create_file_type(name="Image", extension=".jpg")
        company_file = self.create_company_file(
            name="Company Logo",
            file_type=file_type,
            url="https://example.com/logo.jpg"
        )

        assert company_file.id is not None
        assert company_file.name == "Company Logo"
        assert company_file.file_type == file_type
        assert company_file.url == "https://example.com/logo.jpg"
        assert company_file.created_by is not None

    def test_create_company(self):
        """Test creating a company directly."""
        tenant = self.create_tenant(name="Test Tenant")
        company = self.create_company(
            name="Test Company",
            email="test@company.com",
            phone="123-456-7890",
            tenant=tenant
        )

        assert company.id is not None
        assert company.name == "Test Company"
        assert company.email == "test@company.com"
        assert company.phone == "123-456-7890"
        assert company.tenant == tenant
        assert company.created_by is not None

    def test_create_location(self):
        """Test creating a location directly."""
        tenant = self.create_tenant(name="Test Tenant")
        location = self.create_location(
            name="New York",
            code="NYC",
            zip_code="10001",
            tenant=tenant
        )

        assert location.id is not None
        assert location.name == "New York"
        assert location.code == "NYC"
        assert location.zip == "10001"
        assert location.tenant == tenant
        assert location.created_by is not None

    def test_create_event(self):
        """Test creating an event directly."""
        tenant = self.create_tenant(name="Test Tenant")
        event = self.create_event(
            name="Test Event",
            tenant=tenant,
            address="123 Event St"
        )

        assert event.id is not None
        assert event.name == "Test Event"
        assert event.address == "123 Event St"
        assert event.tenant == tenant
        assert event.created_by is not None

    def test_create_ambassador(self):
        """Test creating an ambassador directly."""
        roles = self.setup_default_roles()
        user = self.create_user(
            username="ambassador@test.com",
            email="ambassador@test.com",
            role=roles['ambassador']
        )
        ambassador = self.create_ambassador(user=user)

        assert ambassador.id is not None
        assert ambassador.user == user
        assert ambassador.created_by is not None

    def test_create_job_title(self):
        """Test creating a job title directly."""
        tenant = self.create_tenant(name="Test Tenant")
        job_title = self.create_job_title(
            name="Software Engineer", tenant=tenant)

        assert job_title.id is not None
        assert job_title.name == "Software Engineer"
        assert job_title.tenant == tenant
        assert job_title.created_by is not None

    def test_create_rate_type(self):
        """Test creating a rate type directly."""
        tenant = self.create_tenant(name="Test Tenant")
        rate_type = self.create_rate_type(name="Hourly", tenant=tenant)

        assert rate_type.id is not None
        assert rate_type.name == "Hourly"
        assert rate_type.tenant == tenant
        assert rate_type.created_by is not None

    def test_create_rate(self):
        """Test creating a rate directly."""
        tenant = self.create_tenant(name="Test Tenant")
        rate_type = self.create_rate_type(name="Hourly", tenant=tenant)
        rate = self.create_rate(
            amount=50.0, rate_type=rate_type, tenant=tenant)

        assert rate.id is not None
        assert rate.amount == 50.0
        assert rate.rate_type == rate_type
        assert rate.tenant == tenant
        assert rate.created_by is not None

    def test_create_job(self):
        """Test creating a job directly with all dependencies."""
        tenant = self.create_tenant(name="Test Tenant")
        company = self.create_company(
            name="Test Company",
            email="test@company.com",
            phone="123-456-7890",
            tenant=tenant
        )
        location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=tenant
        )
        event = self.create_event(
            name="Test Event",
            tenant=tenant,
            address="123 Event St"
        )
        job_title = self.create_job_title(
            name="Software Engineer", tenant=tenant)

        job = self.create_job(
            name="Test Job",
            code="JOB-001",
            address="123 Job St",
            company=company,
            event=event,
            job_title=job_title,
            tenant=tenant
        )

        assert job.id is not None
        assert job.name == "Test Job"
        assert job.code == "JOB-001"
        assert job.company == company
        assert job.event == event
        assert job.job_title == job_title
        assert job.tenant == tenant
        assert job.created_by is not None

    def test_create_job_file(self):
        """Test creating a job file directly."""
        tenant = self.create_tenant(name="Test Tenant")
        company = self.create_company(
            name="Test Company",
            email="test@company.com",
            phone="123-456-7890",
            tenant=tenant
        )
        location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=tenant
        )
        event = self.create_event(
            name="Test Event",
            tenant=tenant,
            address="123 Event St"
        )
        job_title = self.create_job_title(
            name="Software Engineer", tenant=tenant)
        job = self.create_job(
            name="Test Job",
            code="JOB-001",
            address="123 Job St",
            company=company,
            event=event,
            job_title=job_title,
            tenant=tenant
        )
        file_type = self.create_file_type(name="PDF", extension=".pdf")

        job_file = self.create_job_file(
            name="Job Description",
            url="https://example.com/job.pdf",
            job=job,
            file_type=file_type
        )

        assert job_file.id is not None
        assert job_file.name == "Job Description"
        assert job_file.url == "https://example.com/job.pdf"
        assert job_file.job == job
        assert job_file.file_type == file_type
        assert job_file.created_by is not None

    def test_create_job_requirement_type(self):
        """Test creating a job requirement type directly."""
        tenant = self.create_tenant(name="Test Tenant")
        job_requirement_type = self.create_job_requirement_type(
            name="Education",
            tenant=tenant
        )

        assert job_requirement_type.id is not None
        assert job_requirement_type.name == "Education"
        assert job_requirement_type.tenant == tenant
        assert job_requirement_type.created_by is not None

    def test_create_job_requirement(self):
        """Test creating a job requirement directly."""
        tenant = self.create_tenant(name="Test Tenant")
        company = self.create_company(
            name="Test Company",
            email="test@company.com",
            phone="123-456-7890",
            tenant=tenant
        )
        location = self.create_location(
            name="Test Location",
            code="TEST",
            zip_code="12345",
            tenant=tenant
        )
        event = self.create_event(
            name="Test Event",
            tenant=tenant,
            address="123 Event St"
        )
        job_title = self.create_job_title(
            name="Software Engineer", tenant=tenant)
        job = self.create_job(
            name="Test Job",
            code="JOB-001",
            address="123 Job St",
            company=company,
            event=event,
            job_title=job_title,
            tenant=tenant
        )
        job_requirement_type = self.create_job_requirement_type(
            name="Education",
            tenant=tenant
        )

        job_requirement = self.create_job_requirement(
            name="Bachelor's Degree",
            job=job,
            job_requirement_type=job_requirement_type
        )

        assert job_requirement.id is not None
        assert job_requirement.name == "Bachelor's Degree"
        assert job_requirement.job == job
        assert job_requirement.job_requirement_type == job_requirement_type
        assert job_requirement.created_by is not None
