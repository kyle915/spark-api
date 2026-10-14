"""Public clock links: clock in/out over REST, the Sheet mirror, admin GraphQL."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest import mock

import pytest
from asgiref.sync import sync_to_async
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client as DjangoClient
from django.test import override_settings
from django.urls import reverse
from googleapiclient.errors import HttpError

from tenants.tests.base import BaseGraphQLTestCase
from timeclock import services, sheets
from timeclock.models import ClockLink, ClockSession
from timeclock.tests.fake_sheets import FakeSheets

SHEET_ID = "sheet123"


def _in_body(**over):
    body = {
        "firstName": "Dana",
        "lastName": "Reyes",
        "email": "Dana@Example.com",
        "market": "Phoenix",
        "address": "123 Camelback Rd, Phoenix, AZ",
        "gps": {"lat": 33.509123456, "lng": -112.0741234, "accuracy": 12.34},
        "gpsStatus": "ok",
        "timezone": "America/Phoenix",
    }
    body.update(over)
    return body


@pytest.fixture
def fake():
    f = FakeSheets()
    f.add_spreadsheet(SHEET_ID, ["Sheet1"])
    with mock.patch("timeclock.sheets._service", return_value=f), mock.patch(
        "timeclock.sheets.service_account_email", return_value="sa@test.iam.gserviceaccount.com"
    ):
        yield f


@pytest.fixture
def inline_sync():
    """Run the Sheet mirror inline instead of Cloud Tasks / a thread."""
    with mock.patch("timeclock.services._dispatch", side_effect=sheets.sync_session) as m:
        yield m


@pytest.fixture(autouse=True)
def _clear_rate_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db(transaction=True)
class TestPublicClock:
    @pytest.fixture(autouse=True)
    def setup(self, db):
        self.link = ClockLink.objects.create(label="Generic", sheet_id=SHEET_ID)
        self.http = DjangoClient()

    def _post(self, name, body, ip="1.2.3.4"):
        return self.http.post(
            reverse(name, kwargs={"token": self.link.token}),
            data=json.dumps(body),
            content_type="application/json",
            HTTP_X_FORWARDED_FOR=ip,
        )

    def _get(self, session_token=""):
        headers = {"HTTP_X_CLOCK_SESSION": session_token} if session_token else {}
        return self.http.get(reverse("timeclock.public", kwargs={"token": self.link.token}), **headers)

    def test_unknown_token_is_404(self):
        res = self.http.get(reverse("timeclock.public", kwargs={"token": "nope"}))
        assert res.status_code == 404

    def test_context_has_no_brand_without_tenant(self):
        data = self._get().json()
        assert data["label"] == "Generic"
        assert data["active"] is True
        assert data["brand"] is None
        assert data["session"] is None

    def test_clock_in_stores_session_and_gps(self, fake, inline_sync):
        res = self._post("timeclock.clock_in", _in_body())
        assert res.status_code == 201, res.content
        data = res.json()
        s = ClockSession.objects.get()
        assert s.email == "dana@example.com"
        assert (s.clock_in_lat, s.clock_in_lng, s.clock_in_accuracy_m) == (33.509123, -112.074123, 12.3)
        assert s.clock_in_gps == "ok"
        assert s.timezone == "America/Phoenix"
        assert s.ip_address == "1.2.3.4"
        assert data["session"]["id"] == str(s.uuid)
        assert self._get(data["sessionToken"]).json()["session"]["id"] == str(s.uuid)

    def test_gps_denied_still_clocks_in(self, fake, inline_sync):
        res = self._post("timeclock.clock_in", _in_body(gps=None, gpsStatus="denied"))
        assert res.status_code == 201
        s = ClockSession.objects.get()
        assert s.clock_in_lat is None and s.clock_in_gps == "denied"
        row = fake.grid(SHEET_ID, sheets.TAB)[1]
        assert row[sheets.HEADER.index("Clock In Map")] == "'GPS denied"

    def test_bad_email_and_missing_fields(self, fake):
        assert self._post("timeclock.clock_in", _in_body(email="not-an-email")).json()["error"] == "bad_email"
        assert self._post("timeclock.clock_in", _in_body(market="  ")).json()["error"] == "market_required"
        assert not ClockSession.objects.exists()

    def test_second_open_session_for_same_email_is_blocked(self, fake, inline_sync):
        assert self._post("timeclock.clock_in", _in_body()).status_code == 201
        res = self._post("timeclock.clock_in", _in_body(email="dana@example.com"))
        assert res.status_code == 409
        assert res.json()["error"] == "already_clocked_in"
        assert res.json()["openSession"]["firstName"] == "Dana"
        assert ClockSession.objects.count() == 1

    def test_clock_out_with_device_token_updates_same_row(self, fake, inline_sync):
        token = self._post("timeclock.clock_in", _in_body()).json()["sessionToken"]
        s = ClockSession.objects.get()
        ClockSession.objects.filter(pk=s.pk).update(clock_in_at=s.clock_in_at - timedelta(hours=2, minutes=5))
        res = self._post(
            "timeclock.clock_out",
            {"sessionToken": token, "gps": {"lat": 33.5, "lng": -112.07, "accuracy": 8}},
        )
        assert res.status_code == 200, res.content
        assert res.json()["session"]["durationMinutes"] == 125
        grid = fake.grid(SHEET_ID, sheets.TAB)
        assert len(grid) == 2, "clock-out must rewrite the clock-in row, not append"
        row = dict(zip(sheets.HEADER, grid[1]))
        assert row["Status"] == "Clocked out"
        assert row["Duration (h:mm)"] == "'2:05"
        assert row["Hours"] == "2.08"
        assert row["Clock Out Map"] == "https://www.google.com/maps?q=33.5,-112.07"
        assert row["Session ID"] == str(s.uuid)
        assert fake.calls.count("append") == 1

    def test_clock_out_by_email_when_phone_lost_its_session(self, fake, inline_sync):
        self._post("timeclock.clock_in", _in_body())
        res = self._post("timeclock.clock_out", {"email": " DANA@example.com ", "gpsStatus": "denied"})
        assert res.status_code == 200
        s = ClockSession.objects.get()
        assert s.clock_out_at is not None and s.clock_out_gps == "denied"

    def test_clock_out_errors(self, fake, inline_sync):
        assert self._post("timeclock.clock_out", {}).json()["error"] == "no_session"
        assert self._post("timeclock.clock_out", {"email": "x@y.co"}).status_code == 404
        token = self._post("timeclock.clock_in", _in_body()).json()["sessionToken"]
        assert self._post("timeclock.clock_out", {"sessionToken": token}).status_code == 200
        again = self._post("timeclock.clock_out", {"sessionToken": token})
        assert again.status_code == 409 and again.json()["error"] == "already_clocked_out"

    def test_session_token_from_another_link_is_ignored(self, fake, inline_sync):
        token = self._post("timeclock.clock_in", _in_body()).json()["sessionToken"]
        other = ClockLink.objects.create(label="Other")
        res = self.http.get(
            reverse("timeclock.public", kwargs={"token": other.token}), HTTP_X_CLOCK_SESSION=token
        )
        assert res.json()["session"] is None

    @override_settings(CORS_ALLOW_ALL_ORIGINS=False, CORS_ALLOWED_ORIGINS=["https://client.igniteproductions.co"])
    def test_browser_preflight_allows_session_header(self, fake):
        res = DjangoClient().options(
            reverse("timeclock.public", args=[self.link.token]),
            HTTP_ORIGIN="https://client.igniteproductions.co",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="GET",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-clock-session",
        )
        assert "x-clock-session" in res["access-control-allow-headers"]

    def test_inactive_link(self, fake):
        self.link.is_active = False
        self.link.save()
        assert self._get().json()["active"] is False
        res = self._post("timeclock.clock_in", _in_body())
        assert res.status_code == 410 and res.json()["error"] == "inactive"

    def test_clock_in_is_rate_limited_per_ip(self, fake, inline_sync):
        for i in range(20):
            self._post("timeclock.clock_in", _in_body(email=f"w{i}@example.com"))
        res = self._post("timeclock.clock_in", _in_body(email="late@example.com"))
        assert res.status_code == 429
        assert self._post("timeclock.clock_in", _in_body(email="late@example.com"), ip="5.6.7.8").status_code == 201

    def test_typed_formula_is_written_as_text(self, fake, inline_sync):
        self._post("timeclock.clock_in", _in_body(firstName='=HYPERLINK("http://x")'))
        row = fake.grid(SHEET_ID, sheets.TAB)[1]
        assert row[1] == "'=HYPERLINK(\"http://x\")"

    def test_sheet_failure_never_blocks_clock_in_and_sweep_retries(self, fake, inline_sync):
        fake.fail_with = HttpError(mock.Mock(status=403), b"denied")
        res = self._post("timeclock.clock_in", _in_body())
        assert res.status_code == 201
        s = ClockSession.objects.get()
        assert s.sheet_dirty and "share the Sheet" in s.sheet_error
        fake.fail_with = None
        assert sheets.sync_dirty() == {"attempted": 1, "synced": 1, "failed": 0}
        s.refresh_from_db()
        assert not s.sheet_dirty and s.sheet_error == "" and s.sheet_row == 2

    def test_row_found_again_after_someone_sorts_the_sheet(self, fake, inline_sync):
        token = self._post("timeclock.clock_in", _in_body()).json()["sessionToken"]
        self._post("timeclock.clock_in", _in_body(email="b@example.com"))
        grid = fake.grid(SHEET_ID, sheets.TAB)
        grid[1], grid[2] = grid[2], grid[1]
        self._post("timeclock.clock_out", {"sessionToken": token})
        rows = {r[-1]: r for r in fake.grid(SHEET_ID, sheets.TAB)[1:]}
        dana = ClockSession.objects.get(email="dana@example.com")
        assert rows[str(dana.uuid)][sheets.HEADER.index("Status")] == "Clocked out"
        assert len(rows) == 2

    def test_no_sheet_means_no_dispatch(self):
        self.link.sheet_id = ""
        self.link.save()
        with mock.patch("timeclock.services._dispatch") as dispatch:
            assert self._post("timeclock.clock_in", _in_body()).status_code == 201
        dispatch.assert_not_called()

    def test_dispatch_falls_back_to_thread_when_tasks_are_off(self):
        with mock.patch("timeclock.services.cloud_tasks.enqueue", return_value=False), mock.patch(
            "timeclock.services.threading.Thread"
        ) as thread:
            services._dispatch(42)
        thread.assert_called_once()
        assert thread.call_args.kwargs["args"] == (42,)


@pytest.mark.django_db(transaction=True)
class TestTaskAndCron:
    @override_settings(CLOUD_TASKS_SECRET="s3cret")
    def test_task_handler_requires_secret_and_syncs(self, fake):
        link = ClockLink.objects.create(label="T", sheet_id=SHEET_ID)
        with mock.patch("timeclock.services._dispatch"):
            s = services.clock_in(link, _in_body())
        http = DjangoClient()
        url = reverse("tasks.clock_sheet_sync")
        assert http.post(url, data=json.dumps({"session_id": s.id}), content_type="application/json").status_code == 403
        res = http.post(
            url,
            data=json.dumps({"session_id": s.id}),
            content_type="application/json",
            HTTP_X_TASKS_SECRET="s3cret",
        )
        assert res.status_code == 200 and res.json()["ok"] is True
        assert len(fake.grid(SHEET_ID, sheets.TAB)) == 2

    @override_settings(INTERNAL_CRON_SECRET="cron")
    def test_cron_create_is_dry_run_by_default(self, fake):
        http = DjangoClient()
        url = reverse("timeclock-cron-clock-links")
        body = {"action": "create", "label": "Generic clock — template"}
        res = http.post(url, data=body, HTTP_X_CRON_SECRET="cron")
        assert res.status_code == 200 and "DRY RUN" in res.json()["log"]
        assert not ClockLink.objects.exists()
        drive = _drive_mock()
        with mock.patch("timeclock.sheets._drive_service", return_value=drive):
            res = http.post(url, data={**body, "apply": "true"}, HTTP_X_CRON_SECRET="cron")
        assert res.status_code == 200, res.content
        share = drive.permissions.return_value.create.call_args.kwargs
        assert share["body"] == {"type": "user", "role": "writer", "emailAddress": "kyle@igniteproductions.co"}
        link = ClockLink.objects.get()
        assert link.sheet_id.startswith("created")
        assert fake.grid(link.sheet_id, sheets.TAB)[0] == sheets.HEADER
        # Re-running reuses the link.
        http.post(url, data={**body, "apply": "true"}, HTTP_X_CRON_SECRET="cron")
        assert ClockLink.objects.count() == 1

    def test_purge_session_removes_row_then_session(self, fake):
        link = ClockLink.objects.create(label="P", sheet_id=SHEET_ID)
        with mock.patch("timeclock.services._dispatch", side_effect=sheets.sync_session):
            keep = services.clock_in(link, _in_body(email="keep@example.com"))
            gone = services.clock_in(link, _in_body(email="gone@example.com"))
        call_command("clock_links", action="purge-session", session_uuid=str(gone.uuid))
        assert ClockSession.objects.count() == 2
        call_command("clock_links", action="purge-session", session_uuid=str(gone.uuid), apply=True)
        assert list(ClockSession.objects.values_list("id", flat=True)) == [keep.id]
        ids = [r[-1] for r in fake.grid(SHEET_ID, sheets.TAB)[1:]]
        assert ids == [str(keep.uuid)]


def _drive_mock():
    drive = mock.Mock()
    drive.permissions.return_value.create.return_value.execute.return_value = {}
    return drive


@pytest.mark.django_db(transaction=True)
class TestClockLinkAdmin(BaseGraphQLTestCase):
    CREATE = """
    mutation C($input: CreateClockLinkInput!) {
      createClockLink(input: $input) {
        success message
        link { id label url sheetUrl isActive tenantName sessionCount }
      }
    }
    """
    LIST = "query { clockLinks { available serviceAccountEmail links { id label openCount syncPending } } }"
    SESSIONS = """
    query S($id: ID!) { clockSessions(linkId: $id) {
      id firstName email clockInMapUrl clockOutAt durationMinutes sheetSynced } }
    """
    DELETE = """
    mutation D($input: DeleteClockSessionInput!) { deleteClockSession(input: $input) { success message } }
    """

    @pytest.fixture(autouse=True)
    def setup(self, db):
        from config.schema_client import schema_clients

        self.roles = self.setup_default_roles()
        self.schema = schema_clients
        self.endpoint_path = "/api/v1/graphql/clients"

    async def _user(self, role, email=None):
        uid = uuid.uuid4().hex[:6]
        email = email or f"{role}-{uid}@test.com"
        return await sync_to_async(self.create_user)(
            username=email, email=email, role=self.roles[role]
        )

    @pytest.mark.asyncio
    async def test_staff_create_list_sessions_delete(self, fake):
        admin = await self._user("spark_admin")
        with mock.patch("timeclock.sheets._drive_service", return_value=_drive_mock()):
            res = await self._execute_mutation(
                self.CREATE, {"input": {"label": "  Acme   shift clock "}}, user=admin
            )
        assert res.errors is None, res.errors
        out = res.data["createClockLink"]
        assert out["success"] and "kyle@igniteproductions.co" in out["message"]
        link = out["link"]
        assert link["label"] == "Acme shift clock"
        assert link["url"].startswith("https://client.igniteproductions.co/clock/")
        assert link["sheetUrl"].startswith("https://docs.google.com/spreadsheets/d/created")

        db_link = await sync_to_async(ClockLink.objects.get)(uuid=link["id"])
        with mock.patch("timeclock.services._dispatch", side_effect=sheets.sync_session):
            s = await sync_to_async(services.clock_in)(db_link, _in_body())

        listed = await self._execute_mutation(self.LIST, {}, user=admin)
        assert listed.data["clockLinks"]["available"] is True
        assert listed.data["clockLinks"]["serviceAccountEmail"] == "sa@test.iam.gserviceaccount.com"
        assert listed.data["clockLinks"]["links"][0]["openCount"] == 1

        rows = await self._execute_mutation(self.SESSIONS, {"id": link["id"]}, user=admin)
        assert rows.errors is None, rows.errors
        got = rows.data["clockSessions"][0]
        assert got["email"] == "dana@example.com" and got["sheetSynced"] is True
        assert got["clockInMapUrl"] == "https://www.google.com/maps?q=33.509123,-112.074123"

        deleted = await self._execute_mutation(self.DELETE, {"input": {"sessionId": str(s.uuid)}}, user=admin)
        assert deleted.data["deleteClockSession"]["success"] is True
        assert not await sync_to_async(ClockSession.objects.exists)()
        assert fake.grid(db_link.sheet_id, sheets.TAB) == [sheets.HEADER]

    @pytest.mark.asyncio
    async def test_pasted_sheet_needs_service_account_access(self, fake):
        admin = await self._user("spark_admin")
        fake.fail_with = HttpError(mock.Mock(status=403), b"nope")
        res = await self._execute_mutation(
            self.CREATE,
            {"input": {"label": "Pasted", "sheetMode": "paste",
                       "sheetUrl": "https://docs.google.com/spreadsheets/d/abcDEF123/edit"}},
            user=admin,
        )
        out = res.data["createClockLink"]
        assert out["link"]["sheetUrl"] is None
        assert "sa@test.iam.gserviceaccount.com" in out["message"]

    @pytest.mark.asyncio
    async def test_clients_get_nothing(self, fake):
        client = await self._user("client")
        listed = await self._execute_mutation(self.LIST, {}, user=client)
        assert listed.data["clockLinks"] == {"available": False, "serviceAccountEmail": None, "links": []}
        res = await self._execute_mutation(self.CREATE, {"input": {"label": "Nope"}}, user=client)
        assert res.errors and "staff only" in res.errors[0].message
        assert not await sync_to_async(ClockLink.objects.exists)()
