"""End-to-end coverage for the Consumer Receipt Validation backend.

Two flows, exercised against the real surfaces:

1. PUBLIC SUBMIT — a shopper POSTs a base64 receipt image to the public,
   no-auth endpoint keyed off a per-event token. It must store the image
   to GCS (mocked here) and create a `pending` ConsumerReceipt. We also
   check the GET token-resolve returns the event/brand display info.

2. ADMIN REVIEW — the `reviewReceipt` mutation (clients schema) flips the
   receipt to `validated` and stamps reviewed_by / reviewed_at / note. We
   also confirm the tenant-scoped `receipts` query surfaces the pending
   receipt with its publicUrl.

GCS is patched at `receipts.views.upload_bytes` so no real bucket I/O
happens; everything else runs against the real models / schema.
"""

from datetime import datetime, timedelta, timezone as _tz
from unittest.mock import patch

import pytest

from ambassadors.tests.base import AmbassadorsGraphQLTestCase
from receipts import models as receipt_models
from receipts.tokens import make_event_receipt_token, verify_event_receipt_token


REVIEW_RECEIPT_MUTATION = """
mutation ReviewReceipt($input: ReviewReceiptInput!) {
  reviewReceipt(input: $input) {
    success
    message
    receipt {
      id
      status
      reviewNote
      reviewedAt
      reviewedBy { email }
    }
  }
}
"""

RECEIPTS_QUERY = """
query Receipts($tenantId: ID, $status: String, $eventId: ID, $first: Int) {
  receipts(
    filters: { tenantId: $tenantId, status: $status, eventId: $eventId }
    first: $first
  ) {
    totalCount
    edges {
      node {
        id
        uuid
        status
        consumerName
        storeName
        amount
        publicUrl
        eventName
      }
    }
  }
}
"""

EVENT_LINK_QUERY = """
query Link($eventId: ID!) {
  eventReceiptUploadLink(eventId: $eventId) {
    eventId
    token
    url
  }
}
"""


@pytest.mark.django_db(transaction=True)
class TestConsumerReceiptFlow(AmbassadorsGraphQLTestCase):
    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

        self.tenant = self.create_tenant(name="Liquid Death")
        self.spark_admin = self.create_user(
            username="admin-receipts",
            email="admin-receipts@test.com",
            role=self.roles["spark_admin"],
        )

        now = datetime.now(_tz.utc)
        self.event = self.create_event(
            name="Whole Foods Venice Sampling",
            tenant=self.tenant,
            address="123 Abbot Kinney",
            date=now,
            start_time=now,
            end_time=now + timedelta(hours=4),
            notes="Liquid Death Sparkling Water",
        )

    # ------------------------------------------------------------------
    # Token round-trips.
    # ------------------------------------------------------------------
    def test_token_round_trips_to_event_id(self):
        token = make_event_receipt_token(self.event.id)
        assert verify_event_receipt_token(token) == self.event.id

    # ------------------------------------------------------------------
    # 1. Public submit creates a pending receipt.
    # ------------------------------------------------------------------
    def test_public_submit_creates_pending_receipt(self):
        import base64

        from django.test import Client

        token = make_event_receipt_token(self.event.id)
        # 1x1 transparent PNG.
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        payload = {
            "image": base64.b64encode(png).decode("ascii"),
            "contentType": "image/png",
            "consumerName": "Jane Shopper",
            "consumerEmail": "jane@example.com",
            "storeName": "Whole Foods Venice",
            "purchaseDate": "2026-05-29",
            "amount": "12.49",
            "product": "Liquid Death 12-pack",
        }

        client = Client()
        with patch("receipts.views.upload_bytes") as mock_upload:
            resp = client.post(
                f"/api/public/receipts/{token}/submit",
                data=payload,
                content_type="application/json",
            )

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"ok": True}

        # Image was pushed to GCS under the expected prefix.
        assert mock_upload.call_count == 1
        blob_name = mock_upload.call_args.args[0]
        assert blob_name.startswith(
            f"consumer-receipts/{self.tenant.id}/{self.event.id}/"
        )

        receipt = receipt_models.ConsumerReceipt.objects.get()
        assert receipt.status == receipt_models.ConsumerReceipt.STATUS_PENDING
        assert receipt.tenant_id == self.tenant.id
        assert receipt.event_id == self.event.id
        assert receipt.image == blob_name
        assert receipt.consumer_name == "Jane Shopper"
        assert receipt.store_name == "Whole Foods Venice"
        assert str(receipt.amount) == "12.49"
        assert receipt.reviewed_by_id is None
        assert receipt.reviewed_at is None

    def test_public_submit_accepts_multipart(self):
        import base64
        import io

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import Client

        token = make_event_receipt_token(self.event.id)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        upload = SimpleUploadedFile("receipt.png", png, content_type="image/png")

        client = Client()
        with patch("receipts.views.upload_bytes") as mock_upload:
            resp = client.post(
                f"/api/public/receipts/{token}/submit",
                data={
                    "image": upload,
                    "consumerName": "Multipart Shopper",
                    "storeName": "Erewhon",
                },
            )

        assert resp.status_code == 200, resp.content
        assert resp.json() == {"ok": True}
        assert mock_upload.call_count == 1

        receipt = receipt_models.ConsumerReceipt.objects.get()
        assert receipt.status == receipt_models.ConsumerReceipt.STATUS_PENDING
        assert receipt.consumer_name == "Multipart Shopper"
        assert receipt.store_name == "Erewhon"

    def test_public_get_resolves_event_display_info(self):
        from django.test import Client

        token = make_event_receipt_token(self.event.id)
        client = Client()
        resp = client.get(f"/api/public/receipts/{token}")
        assert resp.status_code == 200, resp.content
        body = resp.json()["event"]
        assert body["eventName"] == "Whole Foods Venice Sampling"
        assert body["brandName"] == "Liquid Death"
        assert body["product"] == "Liquid Death Sparkling Water"

    def test_public_submit_rejects_bad_token(self):
        from django.test import Client

        client = Client()
        resp = client.post(
            "/api/public/receipts/not-a-real-token/submit",
            data={"image": "x", "contentType": "image/png"},
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "invalid"

    # ------------------------------------------------------------------
    # 2. reviewReceipt flips status + stamps reviewer.
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_review_receipt_validates_and_stamps_reviewer(self):
        from asgiref.sync import sync_to_async

        receipt = await sync_to_async(
            receipt_models.ConsumerReceipt.objects.create
        )(
            tenant=self.tenant,
            event=self.event,
            image="consumer-receipts/x/y/z.png",
            status=receipt_models.ConsumerReceipt.STATUS_PENDING,
        )

        result = await self._execute_mutation_authenticated(
            REVIEW_RECEIPT_MUTATION,
            {
                "input": {
                    "id": str(receipt.id),
                    "status": "validated",
                    "note": "Receipt is legit.",
                }
            },
            self.spark_admin,
            self.endpoint_path,
        )

        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["reviewReceipt"]
        assert payload["success"] is True
        assert payload["receipt"]["status"] == "validated"
        assert payload["receipt"]["reviewNote"] == "Receipt is legit."
        assert payload["receipt"]["reviewedAt"] is not None
        assert payload["receipt"]["reviewedBy"]["email"] == "admin-receipts@test.com"

        # DB reflects the review.
        refreshed = await sync_to_async(
            receipt_models.ConsumerReceipt.objects.get
        )(id=receipt.id)
        assert refreshed.status == receipt_models.ConsumerReceipt.STATUS_VALIDATED
        assert refreshed.reviewed_by_id == self.spark_admin.id
        assert refreshed.reviewed_at is not None
        assert refreshed.review_note == "Receipt is legit."

    @pytest.mark.asyncio
    async def test_review_receipt_rejects_invalid_status(self):
        from asgiref.sync import sync_to_async

        receipt = await sync_to_async(
            receipt_models.ConsumerReceipt.objects.create
        )(
            tenant=self.tenant,
            event=self.event,
            image="consumer-receipts/x/y/z.png",
            status=receipt_models.ConsumerReceipt.STATUS_PENDING,
        )

        result = await self._execute_mutation_authenticated(
            REVIEW_RECEIPT_MUTATION,
            {"input": {"id": str(receipt.id), "status": "pending"}},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        payload = result.data["reviewReceipt"]
        assert payload["success"] is False
        assert "validated" in payload["message"]

    # ------------------------------------------------------------------
    # receipts query + eventReceiptUploadLink.
    # ------------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_receipts_query_returns_tenant_scoped_pending(self):
        from asgiref.sync import sync_to_async

        await sync_to_async(receipt_models.ConsumerReceipt.objects.create)(
            tenant=self.tenant,
            event=self.event,
            image="consumer-receipts/x/y/z.png",
            status=receipt_models.ConsumerReceipt.STATUS_PENDING,
            consumer_name="Jane Shopper",
            store_name="Whole Foods Venice",
        )

        result = await self._execute_query_authenticated(
            RECEIPTS_QUERY,
            {"tenantId": str(self.tenant.id), "status": "pending", "first": 50},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        conn = result.data["receipts"]
        assert conn["totalCount"] == 1
        node = conn["edges"][0]["node"]
        assert node["status"] == "pending"
        assert node["consumerName"] == "Jane Shopper"
        assert node["storeName"] == "Whole Foods Venice"
        assert node["eventName"] == "Whole Foods Venice Sampling"
        assert node["publicUrl"].endswith("consumer-receipts/x/y/z.png")

    @pytest.mark.asyncio
    async def test_event_receipt_upload_link(self):
        result = await self._execute_query_authenticated(
            EVENT_LINK_QUERY,
            {"eventId": str(self.event.id)},
            self.spark_admin,
            self.endpoint_path,
        )
        assert result.errors is None, f"errored: {result.errors}"
        link = result.data["eventReceiptUploadLink"]
        assert link["eventId"] == str(self.event.id)
        assert link["token"]
        assert f"/api/public/receipts/{link['token']}" in link["url"]
        # Token in the link resolves back to this event.
        assert verify_event_receipt_token(link["token"]) == self.event.id
