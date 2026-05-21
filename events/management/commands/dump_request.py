"""Diagnostic — dump a Request by UUID with every visible field, to
find why /request/view/<uuid> renders blanks."""
from django.core.management.base import BaseCommand
from events import models


class Command(BaseCommand):
    help = "Dump a Request's fields by UUID for debugging."

    def add_arguments(self, parser):
        parser.add_argument("uuid", type=str)

    def handle(self, *args, **opts):
        try:
            req = (
                models.Request.objects
                .select_related(
                    "tenant", "retailer", "distributor",
                    "location", "state", "request_type",
                    "status", "rmm_asigned", "event",
                    "created_by",
                )
                .get(uuid=opts["uuid"])
            )
        except models.Request.DoesNotExist:
            self.stdout.write(self.style.ERROR("Not found"))
            return

        self.stdout.write(self.style.SUCCESS(f"Request #{req.id} {req.uuid}"))
        rows = [
            ("name", req.name),
            ("date", req.date),
            ("start_time", req.start_time),
            ("end_time", req.end_time),
            ("address", req.address),
            ("coordinates", req.coordinates),
            ("notes", (req.notes or "")[:80]),
            ("tenant_id", req.tenant_id),
            ("tenant.name", getattr(req.tenant, "name", None)),
            ("retailer_id", req.retailer_id),
            ("retailer.name", getattr(req.retailer, "name", None)),
            ("distributor_id", req.distributor_id),
            ("location_id", req.location_id),
            ("state_id", req.state_id),
            ("state.code", getattr(req.state, "code", None)),
            ("request_type_id", req.request_type_id),
            ("request_type.name", getattr(req.request_type, "name", None)),
            ("status_id", req.status_id),
            ("status.name", getattr(req.status, "name", None)),
            ("rmm_asigned_id", req.rmm_asigned_id),
            ("rmm_asigned.email", getattr(req.rmm_asigned, "email", None)),
            ("event_id", req.event_id),
            ("event.name", getattr(req.event, "name", None)),
            ("created_by_id", req.created_by_id),
            ("created_by.email", getattr(req.created_by, "email", None)),
            ("created_at", req.created_at),
            ("deleted_at", getattr(req, "deleted_at", None)),
            ("requestor_email", req.requestor_email),
        ]
        for k, v in rows:
            self.stdout.write(f"  {k:30s} = {v!r}")
