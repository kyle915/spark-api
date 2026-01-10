import pytest
from django.db import IntegrityError

from tenants.models import Tenant, TenantTheme
from tenants.tests.base import BaseGraphQLTestCase


@pytest.mark.django_db
class TestTenantThemeModel(BaseGraphQLTestCase):
    """Tests for the TenantTheme model."""

    def test_default_values_for_new_theme(self):
        """A new TenantTheme uses the default theme JSON and dark scheme."""
        tenant = self.create_tenant(name="Theme Tenant")
        system_user = self.get_system_user()

        theme = TenantTheme.objects.create(
            tenant=tenant,
            created_by=system_user,
            updated_by=system_user,
        )

        assert theme.tenant == tenant
        assert theme.name == "default"
        assert theme.color_scheme == "dark"

        # css_variables should contain some core DaisyUI variables
        vars = theme.css_variables
        assert isinstance(vars, dict)
        assert vars.get("color-scheme") == "dark"
        assert "--color-primary" in vars
        assert "--color-base-100" in vars

    def test_unique_theme_per_tenant_and_color_scheme(self):
        """
        A tenant can only have one theme per color_scheme.

        Creating a second theme with the same (tenant, color_scheme) should
        raise an IntegrityError.
        """
        tenant = self.create_tenant(name="Unique Theme Tenant")
        system_user = self.get_system_user()

        TenantTheme.objects.create(
            tenant=tenant,
            color_scheme="dark",
            created_by=system_user,
            updated_by=system_user,
        )

        with pytest.raises(IntegrityError):
            TenantTheme.objects.create(
                tenant=tenant,
                color_scheme="dark",  # duplicate scheme for same tenant
                created_by=system_user,
                updated_by=system_user,
            )

    def test_multiple_color_schemes_allowed_for_same_tenant(self):
        """A tenant can have both light and dark themes."""
        tenant = self.create_tenant(name="Multi Scheme Tenant")
        system_user = self.get_system_user()

        dark_theme = TenantTheme.objects.create(
            tenant=tenant,
            color_scheme="dark",
            name="Dark Theme",
            created_by=system_user,
            updated_by=system_user,
        )

        light_theme = TenantTheme.objects.create(
            tenant=tenant,
            color_scheme="light",
            name="Light Theme",
            created_by=system_user,
            updated_by=system_user,
        )

        assert dark_theme.tenant == tenant
        assert light_theme.tenant == tenant
        assert dark_theme.color_scheme == "dark"
        assert light_theme.color_scheme == "light"
