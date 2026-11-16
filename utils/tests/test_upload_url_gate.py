"""
getUploadUrl: sign-in required on every schema that exposes it, and the
object name is always server-generated (whitelisted prefix + random name +
the requested extension) so a caller can never target an existing object.
Signing is patched out; no GCS call is made.
"""

import re
from unittest.mock import patch

import pytest

from tenants.tests.base import BaseGraphQLTestCase
from utils.graphql.gcs_schema import build_upload_blob_name

QUERY = """
query Upload($filePath: String!, $contentType: String!) {
  getUploadUrl(filePath: $filePath, contentType: $contentType) {
    uploadUrl
    blobName
  }
}
"""

HEX32 = r"[0-9a-f]{32}"


@pytest.mark.parametrize(
    "requested, pattern",
    [
        # Every path the web + mobile apps send today.
        ("recaps/abc-uuid/1712-photo.JPG", rf"^recaps/abc-uuid/{HEX32}\.jpg$"),
        ("recaps/receipts/abc/1712-r.pdf", rf"^recaps/receipts/abc/{HEX32}\.pdf$"),
        ("chat_attachments/thread-1/1712-a.png", rf"^chat_attachments/thread-1/{HEX32}\.png$"),
        ("receipt-campaign-images/hero/1712-x.webp", rf"^receipt-campaign-images/hero/{HEX32}\.webp$"),
        ("products/1712_can.png", rf"^products/{HEX32}\.png$"),
        ("connecteam-imports/1712-recap.pdf", rf"^connecteam-imports/{HEX32}\.pdf$"),
        ("ba/headshot/1712-55.jpg", rf"^ba/headshot/{HEX32}\.jpg$"),
        ("documents/1712.pdf", rf"^documents/{HEX32}\.pdf$"),
        # Anything else lands under uploads/ — never where the caller asked.
        ("tenant-logos/liquid-death.png", rf"^uploads/{HEX32}\.png$"),
        ("../../etc/passwd", rf"^uploads/{HEX32}$"),
        ("", rf"^uploads/{HEX32}$"),
        ("connecteam-imports/jobs/x.json", rf"^connecteam-imports/{HEX32}\.json$"),
        ("recaps/../../secret/x.jpg", rf"^recaps/secret/{HEX32}\.jpg$"),
        ("products/a.ph p", rf"^products/{HEX32}$"),
    ],
)
def test_blob_name_is_server_generated(requested, pattern):
    blob = build_upload_blob_name(requested)
    assert re.match(pattern, blob), blob
    # Two requests for the same path never collide (no overwrite).
    assert build_upload_blob_name(requested) != blob


@pytest.mark.django_db(transaction=True)
class TestGetUploadUrlGate(BaseGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.roles = self.setup_default_roles()
        self.client_user = self.create_user(
            username="upload-client", email="upload-client@test.com",
            role=self.roles["client"],
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

    @pytest.mark.asyncio
    async def test_anonymous_refused_everywhere(self):
        with patch(
            "utils.graphql.gcs_schema.generate_upload_url",
            return_value="https://signed.example/put",
        ) as signer:
            for schema, path in self._schemas():
                self.schema = schema
                result = await self._execute_mutation(
                    QUERY,
                    {"filePath": "products/x.png", "contentType": "image/png"},
                    path,
                )
                assert result.errors, path
                assert (result.data or {}).get("getUploadUrl") is None
            signer.assert_not_called()

    @pytest.mark.asyncio
    async def test_signed_in_caller_gets_server_chosen_name(self):
        with patch(
            "utils.graphql.gcs_schema.generate_upload_url",
            return_value="https://signed.example/put",
        ) as signer:
            for schema, path in self._schemas():
                self.schema = schema
                result = await self._execute_mutation(
                    QUERY,
                    {
                        "filePath": "products/existing-hero.png",
                        "contentType": "image/png",
                    },
                    path,
                    user=self.client_user,
                )
                assert result.errors is None, result.errors
                blob = result.data["getUploadUrl"]["blobName"]
                assert re.match(rf"^products/{HEX32}\.png$", blob), blob
                assert result.data["getUploadUrl"]["uploadUrl"]
                # The URL was signed for the generated name, not the request.
                assert signer.call_args[0][0] == blob
