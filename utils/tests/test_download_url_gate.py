"""
getDownloadUrl: sign-in required on every schema that exposes it (it was
open to anonymous callers). Spark admins (DB-backed) may ask for any object
key; everyone else only for catalog/branding assets
(``NON_ADMIN_DOWNLOAD_PREFIXES``). Keys that are URLs or carry ``.``/``..``
segments are refused for everyone.

No web or mobile screen calls getDownloadUrl today, so nothing legitimate
loses access. No GCS call is made (the bucket is public; the resolver only
formats a public URL).
"""

import pytest

from tenants.tests.base import BaseGraphQLTestCase
from utils.graphql.gcs_schema import normalize_download_path

QUERY = """
query Download($filePath: String!) {
  getDownloadUrl(filePath: $filePath)
}
"""

BUCKET = "spark-test-bucket"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("products/a.png", "products/a.png"),
        ("/products/a.png", "products/a.png"),
        ("  documents/x.pdf ", "documents/x.pdf"),
        ("", None),
        ("   ", None),
        ("products/../documents/x.pdf", None),
        ("products/./a.png", None),
        ("products//a.png", None),
        ("products/", None),
        ("https://storage.googleapis.com/b/documents/x.pdf", None),
        ("//evil.example/x", None),
        ("products\\..\\documents\\x.pdf", None),
    ],
)
def test_normalize_download_path(raw, expected):
    assert normalize_download_path(raw) == expected


@pytest.mark.django_db(transaction=True)
class TestGetDownloadUrlGate(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db, settings):
        settings.GS_BUCKET_NAME = BUCKET
        self.roles = self.setup_default_roles()
        self.admin = self.create_user(
            username="dl-admin", email="dl-admin@test.com",
            role=self.roles["spark_admin"],
        )
        self.client_user = self.create_user(
            username="dl-client", email="dl-client@test.com",
            role=self.roles["client"],
        )
        self.ba_user = self.create_user(
            username="dl-ba", email="dl-ba@test.com",
            role=self.roles["ambassador"],
        )

    def _schemas(self):
        from config.schema_client import schema_clients
        from config.schema_mobile import schema_mobile
        from config.schema_spark import schema_spark

        return [
            (schema_spark, "/api/v1/graphql/spark"),
            (schema_clients, "/api/v1/graphql/clients"),
            (schema_mobile, "/api/v1/graphql/mobile"),
        ]

    async def _get(self, schema, path, file_path, user):
        self.schema = schema
        return await self._execute_mutation(
            QUERY, {"filePath": file_path}, path, user=user
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "file_path", ["products/a.png", "documents/w9.pdf", "ba/headshot/x.jpg"]
    )
    async def test_anonymous_refused_everywhere(self, file_path):
        for schema, path in self._schemas():
            result = await self._get(schema, path, file_path, None)
            assert result.errors, path
            assert result.data is None or result.data.get("getDownloadUrl") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["client", "ba"])
    @pytest.mark.parametrize(
        "file_path",
        [
            "documents/w9.pdf",
            "ba/headshot/x.jpg",
            "users/images/x.png",
            "chat_attachments/t1/a.png",
            "recaps/receipts/abc/r.pdf",
            "recap_files/x.pdf",
            "connecteam-imports/jobs/x.json",
            "uploads/x.png",
            # A traversal out of an allowed prefix is refused, not resolved.
            "products/../documents/w9.pdf",
        ],
    )
    async def test_non_admin_refused_outside_allowlist(self, who, file_path):
        user = self.client_user if who == "client" else self.ba_user
        for schema, path in self._schemas():
            result = await self._get(schema, path, file_path, user)
            assert result.errors, (path, file_path)
            assert result.data is None or result.data.get("getDownloadUrl") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("who", ["client", "ba"])
    @pytest.mark.parametrize(
        "file_path",
        [
            "products/can.png",
            "receipt-campaign-images/hero/x.webp",
            "tenants/images/logo.png",
        ],
    )
    async def test_non_admin_gets_catalog_assets(self, who, file_path):
        user = self.client_user if who == "client" else self.ba_user
        for schema, path in self._schemas():
            result = await self._get(schema, path, file_path, user)
            assert result.errors is None, (path, result.errors)
            assert result.data["getDownloadUrl"] == (
                f"https://storage.googleapis.com/{BUCKET}/{file_path}"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "file_path", ["documents/w9.pdf", "connecteam-imports/x.pdf", "products/a.png"]
    )
    async def test_admin_gets_any_key(self, file_path):
        for schema, path in self._schemas():
            result = await self._get(schema, path, file_path, self.admin)
            assert result.errors is None, (path, result.errors)
            assert result.data["getDownloadUrl"] == (
                f"https://storage.googleapis.com/{BUCKET}/{file_path}"
            )

    @pytest.mark.asyncio
    async def test_admin_with_unhydrated_role_still_allowed(self):
        # The JWT user's role doesn't hydrate in async resolvers; the gate
        # re-reads it from the DB.
        self.admin.role = None
        from config.schema_client import schema_clients

        result = await self._get(
            schema_clients, "/api/v1/graphql/clients", "documents/w9.pdf",
            self.admin,
        )
        assert result.errors is None, result.errors

    @pytest.mark.asyncio
    async def test_url_and_traversal_refused_for_admin_too(self):
        from config.schema_client import schema_clients

        for bad in (
            "https://storage.googleapis.com/other-bucket/x.pdf",
            "products/../../x",
        ):
            result = await self._get(
                schema_clients, "/api/v1/graphql/clients", bad, self.admin
            )
            assert result.errors, bad
