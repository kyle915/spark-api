"""Ops for public clock links. Dry run unless --apply.

    clock_links --action list
    clock_links --action create --label "Generic clock — template" [--tenant-slug x]
                [--sheet-url URL | --share-with a@b.co,c@d.co] [--apply]
    clock_links --action sync [--label L] [--apply]
    clock_links --action read-sheet --label L [--rows 20]
    clock_links --action purge-session --session-uuid U [--apply]

``create`` is idempotent on the exact label: an existing link is reused and
only gets a Sheet if it has none. ``purge-session`` removes the session's
Sheet row (Session ID verified) and then the DB row.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from tenants.models import Tenant
from timeclock import sheets
from timeclock.models import ClockLink, ClockSession
from timeclock.schema import DEFAULT_SHARE_WITH, clock_url
from utils.sheets_mirror import _execute_sheets, _service

ACTIONS = ("list", "create", "sync", "read-sheet", "purge-session")


class Command(BaseCommand):
    help = "List / create / sync / read / purge public clock links. Dry run unless --apply."

    def add_arguments(self, parser):
        parser.add_argument("--action", choices=ACTIONS, default="list")
        parser.add_argument("--label", default="")
        parser.add_argument("--tenant-slug", default="")
        parser.add_argument("--sheet-url", default="")
        parser.add_argument("--share-with", default=DEFAULT_SHARE_WITH)
        parser.add_argument("--session-uuid", default="")
        parser.add_argument("--rows", type=int, default=20)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **opts):
        action = opts["action"]
        handler = {
            "list": self._list,
            "create": self._create,
            "sync": self._sync,
            "read-sheet": self._read_sheet,
            "purge-session": self._purge,
        }[action]
        handler(opts)

    def _say(self, msg: str) -> None:
        self.stdout.write(msg)

    def _describe(self, link: ClockLink) -> None:
        n = link.sessions.count()
        open_n = link.sessions.filter(clock_out_at__isnull=True).count()
        tenant = link.tenant.name if link.tenant_id else "—"
        self._say(
            f"- {link.label} | client={tenant} | active={link.is_active} | "
            f"sessions={n} open={open_n}\n  url={clock_url(link)}\n  sheet={link.sheet_url or '—'}"
        )

    def _link(self, opts) -> ClockLink:
        label = opts["label"].strip()
        if not label:
            raise CommandError("--label is required.")
        link = ClockLink.objects.select_related("tenant").filter(label=label).first()
        if link is None:
            raise CommandError(f"No clock link labelled {label!r}.")
        return link

    def _list(self, opts) -> None:
        links = list(ClockLink.objects.select_related("tenant"))
        self._say(f"{len(links)} clock link(s). Sheets service account: {sheets.service_account_email()}")
        for link in links:
            self._describe(link)

    def _create(self, opts) -> None:
        label = " ".join(opts["label"].split())
        if not label:
            raise CommandError("--label is required.")
        tenant = None
        if opts["tenant_slug"]:
            tenant = Tenant.objects.filter(slug=opts["tenant_slug"]).first() or Tenant.objects.filter(
                request_url_name=opts["tenant_slug"]
            ).first()
            if tenant is None:
                raise CommandError(f"No tenant with slug {opts['tenant_slug']!r}.")
        share = [e.strip().lower() for e in opts["share_with"].split(",") if e.strip()]
        existing = ClockLink.objects.filter(label=label).first()
        sheet_plan = (
            f"attach pasted Sheet {opts['sheet_url']}"
            if opts["sheet_url"]
            else f"create 'Spark Clock — {label}' shared with {', '.join(share)}"
        )
        if existing is not None:
            self._say("Link already exists (reusing it):")
            self._describe(existing)
            if existing.sheet_id:
                return
            self._say(f"It has no Sheet. Would {sheet_plan}.")
        else:
            self._say(
                f"Would create link {label!r} (client={tenant.name if tenant else '—'}) and {sheet_plan}."
            )
        if not opts["apply"]:
            self._say("DRY RUN — re-run with --apply to write.")
            return
        link = existing or ClockLink.objects.create(label=label, tenant=tenant)
        try:
            if opts["sheet_url"]:
                sheets.attach_existing_sheet(link, opts["sheet_url"])
            else:
                sheets.create_sheet(link, share)
        except sheets.SheetSetupError as exc:
            self._say(f"Link saved, Sheet NOT set up: {exc}")
        self._say("APPLIED:")
        self._describe(link)

    def _sync(self, opts) -> None:
        link = self._link(opts) if opts["label"] else None
        qs = ClockSession.objects.filter(sheet_dirty=True).exclude(link__sheet_id="")
        if link is not None:
            qs = qs.filter(link=link)
        self._say(f"{qs.count()} session(s) waiting on the Sheet.")
        if not opts["apply"]:
            self._say("DRY RUN — re-run with --apply to write.")
            return
        result = sheets.sync_dirty(link=link)
        self._say(f"synced={result['synced']} failed={result['failed']} attempted={result['attempted']}")

    def _read_sheet(self, opts) -> None:
        link = self._link(opts)
        if not link.sheet_id:
            raise CommandError("That link has no Sheet.")
        svc = _service()
        if svc is None:
            raise CommandError("No Google credentials on the server.")
        got = _execute_sheets(
            svc.spreadsheets().values().get(
                spreadsheetId=link.sheet_id,
                range=f"'{sheets.TAB}'!A1:{sheets.LAST_COL}{max(1, opts['rows']) + 1}",
            ),
            op="clock read sheet",
        )
        rows = got.get("values") or []
        self._say(f"{link.sheet_url} — {max(0, len(rows) - 1)} data row(s) shown")
        for row in rows:
            self._say(" | ".join(row))

    def _purge(self, opts) -> None:
        uuid = opts["session_uuid"].strip()
        if not uuid:
            raise CommandError("--session-uuid is required.")
        session = ClockSession.objects.select_related("link").filter(uuid=uuid).first()
        if session is None:
            raise CommandError(f"No clock session {uuid}.")
        self._say(
            f"Session {uuid}: {session.full_name} <{session.email}> on {session.link.label}, "
            f"in={session.clock_in_at.isoformat()} out={session.clock_out_at.isoformat() if session.clock_out_at else '—'}"
        )
        if not opts["apply"]:
            self._say("DRY RUN — would remove its Sheet row and delete it. Re-run with --apply.")
            return
        removed = sheets.delete_row(session)
        session.delete()
        self._say(f"APPLIED: Sheet row {'removed' if removed else 'not found'}; session deleted.")
